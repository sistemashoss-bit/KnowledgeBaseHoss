import os
import re
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from livekit.api import AccessToken, VideoGrants
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db, SessionLocal
from app import audit, storage, valkey_client as vk
from app.models import (
    Branch, Conversation, ConversationParticipant, Department,
    Message, MessageAttachment, User, UserZone, Zone,
    ROLE_SUPERADMIN, CONV_DIRECT, CONV_GROUP,
)
from app.templating import templates

MAX_CHAT_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


def _cleanup_old_messages_bg():
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        db.query(Message).filter(Message.created_at < cutoff).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

router = APIRouter(prefix="/messaging", tags=["messaging"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conv_display_name(conv: Conversation, current_user: User) -> str:
    if conv.type == CONV_GROUP:
        return conv.name or "Grupo sin nombre"
    other = next(
        (p.user for p in conv.participants if str(p.user_id) != str(current_user.id)),
        None,
    )
    if other:
        return other.name or other.email
    return "Chat directo"


def _conv_avatar(conv: Conversation, current_user: User) -> str:
    """Returns initials for avatar."""
    name = _conv_display_name(conv, current_user)
    return name[0].upper() if name else "?"


def _user_conversations(user: User, db: Session) -> list[dict]:
    """All conversations the user participates in, ordered by last message."""
    part_conv_ids = (
        db.query(ConversationParticipant.conversation_id)
        .filter(ConversationParticipant.user_id == user.id)
        .subquery()
    )
    convs = (
        db.query(Conversation)
        .filter(Conversation.id.in_(part_conv_ids))
        .options(
            joinedload(Conversation.participants).joinedload(ConversationParticipant.user),
        )
        .all()
    )

    result = []
    for conv in convs:
        last_msg = (
            db.query(Message)
            .filter(Message.conversation_id == conv.id)
            .order_by(Message.created_at.desc())
            .first()
        )
        participant = next(
            (p for p in conv.participants if str(p.user_id) == str(user.id)), None
        )
        unread = 0
        if participant:
            q = db.query(func.count(Message.id)).filter(
                Message.conversation_id == conv.id,
                Message.sender_id != user.id,
            )
            if participant.last_read_at:
                q = q.filter(Message.created_at > participant.last_read_at)
            unread = q.scalar() or 0

        other_avatar_key = None
        if conv.type == CONV_DIRECT:
            other = next(
                (p.user for p in conv.participants if str(p.user_id) != str(user.id)),
                None,
            )
            if other:
                other_avatar_key = other.avatar_key

        result.append(
            {
                "conv": conv,
                "last_msg": last_msg,
                "unread": unread,
                "display_name": _conv_display_name(conv, user),
                "avatar": _conv_avatar(conv, user),
                "other_avatar_key": other_avatar_key,
                "last_at": last_msg.created_at if last_msg else conv.created_at,
            }
        )

    result.sort(key=lambda x: x["last_at"], reverse=True)
    return result


def _visible_users(user: User, db: Session) -> list[User]:
    """Users this user can start a DM with."""
    if user.role == ROLE_SUPERADMIN:
        return db.query(User).filter(User.is_active == True, User.id != user.id).order_by(User.email).all()

    ids: set = set()

    if user.department_id:
        for u in db.query(User).filter(User.department_id == user.department_id, User.is_active == True).all():
            ids.add(u.id)

    if user.branch_id:
        for u in db.query(User).filter(User.branch_id == user.branch_id, User.is_active == True).all():
            ids.add(u.id)

    zone_ids = [uz.zone_id for uz in db.query(UserZone).filter(UserZone.user_id == user.id).all()]
    if zone_ids:
        branch_ids = [b.id for b in db.query(Branch).filter(Branch.zone_id.in_(zone_ids)).all()]
        if branch_ids:
            for u in db.query(User).filter(User.branch_id.in_(branch_ids), User.is_active == True).all():
                ids.add(u.id)

    ids.discard(user.id)
    if not ids:
        return []
    return db.query(User).filter(User.id.in_(ids), User.is_active == True).order_by(User.email).all()


def _get_or_create_direct(user_a_id, user_b_id, db: Session) -> Conversation:
    """Return existing DM or create a new one."""
    a_conv_ids = (
        db.query(ConversationParticipant.conversation_id)
        .filter(ConversationParticipant.user_id == user_a_id)
        .subquery()
    )
    existing = (
        db.query(Conversation)
        .filter(Conversation.type == CONV_DIRECT, Conversation.id.in_(a_conv_ids))
        .join(ConversationParticipant, ConversationParticipant.conversation_id == Conversation.id)
        .filter(ConversationParticipant.user_id == user_b_id)
        .first()
    )
    if existing:
        return existing

    conv = Conversation(
        id=uuid.uuid4(),
        type=CONV_DIRECT,
        created_by=user_a_id,
    )
    db.add(conv)
    db.flush()
    for uid in [user_a_id, user_b_id]:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid))
    db.commit()
    return conv


def _mark_read(conv_id, user_id, db: Session):
    p = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == user_id,
        )
        .first()
    )
    if p:
        p.last_read_at = datetime.utcnow()
        db.commit()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def messaging_index(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    if vk.try_acquire_daily_lock("msg_cleanup_30d"):
        background_tasks.add_task(_cleanup_old_messages_bg)

    conv_list = _user_conversations(current_user, db)
    contacts = _visible_users(current_user, db)
    departments = db.query(Department).order_by(Department.name).all()
    zones = db.query(Zone).order_by(Zone.name).all()
    branches = db.query(Branch).order_by(Branch.name).all()
    all_users = db.query(User).filter(User.is_active == True, User.id != current_user.id).order_by(User.email).all()

    return templates.TemplateResponse(
        request,
        "messaging/index.html",
        {
            "current_user": current_user,
            "conv_list": conv_list,
            "active_conv": None,
            "messages": [],
            "contacts": contacts,
            "departments": departments,
            "zones": zones,
            "branches": branches,
            "all_users": all_users,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.get("/{conv_id}", response_class=HTMLResponse)
def conversation_view(
    conv_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    conv = (
        db.query(Conversation)
        .options(joinedload(Conversation.participants).joinedload(ConversationParticipant.user))
        .filter(Conversation.id == conv_id)
        .first()
    )
    if not conv:
        raise HTTPException(404)

    # Security: must be a participant
    is_participant = any(str(p.user_id) == str(current_user.id) for p in conv.participants)
    if not is_participant:
        raise HTTPException(403)

    _mark_read(conv_id, current_user.id, db)

    messages = (
        db.query(Message)
        .options(joinedload(Message.sender), joinedload(Message.attachments))
        .filter(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
        .limit(200)
        .all()
    )

    conv_list = _user_conversations(current_user, db)
    contacts = _visible_users(current_user, db)
    departments = db.query(Department).order_by(Department.name).all()
    zones = db.query(Zone).order_by(Zone.name).all()
    branches = db.query(Branch).order_by(Branch.name).all()
    all_users = db.query(User).filter(User.is_active == True, User.id != current_user.id).order_by(User.email).all()

    return templates.TemplateResponse(
        request,
        "messaging/index.html",
        {
            "current_user": current_user,
            "conv_list": conv_list,
            "active_conv": conv,
            "active_conv_name": _conv_display_name(conv, current_user),
            "messages": messages,
            "contacts": contacts,
            "departments": departments,
            "zones": zones,
            "branches": branches,
            "all_users": all_users,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.get("/{conv_id}/poll", response_class=HTMLResponse)
def poll_messages(
    conv_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """HTMX polling endpoint — returns message feed partial."""
    if not current_user:
        raise HTTPException(401)

    is_participant = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == current_user.id,
        )
        .first()
    )
    if not is_participant:
        raise HTTPException(403)

    messages = (
        db.query(Message)
        .options(joinedload(Message.sender), joinedload(Message.attachments))
        .filter(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
        .limit(200)
        .all()
    )
    _mark_read(conv_id, current_user.id, db)

    conv_type = db.query(Conversation.type).filter(Conversation.id == conv_id).scalar()
    return templates.TemplateResponse(
        request,
        "messaging/_feed.html",
        {"messages": messages, "current_user": current_user, "is_group": conv_type == CONV_GROUP},
    )


@router.get("/{conv_id}/call/token")
def call_token(
    conv_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    is_participant = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == current_user.id,
        )
        .first()
    )
    if not is_participant:
        raise HTTPException(403)

    room_name = f"conv-{conv_id}"
    token = (
        AccessToken(
            os.getenv("LIVEKIT_API_KEY", ""),
            os.getenv("LIVEKIT_API_SECRET", ""),
        )
        .with_identity(str(current_user.id))
        .with_name(current_user.name or current_user.email)
        .with_grants(VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
    return JSONResponse({
        "token": token,
        "url": os.getenv("LIVEKIT_URL", ""),
        "room": room_name,
    })


@router.post("/{conv_id}/send", response_class=HTMLResponse)
async def send_message(
    conv_id: str,
    content: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    is_participant = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == current_user.id,
        )
        .first()
    )
    if not is_participant:
        raise HTTPException(403)

    content = content.strip()
    valid_files = [f for f in files if f.filename]
    if not content and not valid_files:
        raise HTTPException(400)

    msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_id=current_user.id,
        content=content,
    )
    db.add(msg)
    db.flush()

    for f in valid_files:
        data = await f.read()
        if len(data) > MAX_CHAT_FILE_BYTES:
            continue
        safe = _safe_filename(f.filename)
        key = f"chats/{conv_id}/{msg.id}/{uuid.uuid4()}_{safe}"
        storage.upload_chat_file(key, data, f.content_type or "application/octet-stream")
        db.add(MessageAttachment(
            id=uuid.uuid4(),
            message_id=msg.id,
            uploaded_by=current_user.id,
            filename=f.filename,
            file_key=key,
            content_type=f.content_type or "application/octet-stream",
            file_size=len(data),
        ))

    _mark_read(conv_id, current_user.id, db)
    db.commit()

    if valid_files:
        audit.log_action(
            "chat_attachment", user=current_user, request=request,
            resource_type="conversation", resource_id=conv_id,
            details=f"files={len(valid_files)} names={','.join(f.filename for f in valid_files[:5])}",
        )
    return HTMLResponse("", headers={"HX-Trigger": "refreshFeed"})


@router.get("/{conv_id}/attachments/{att_id}/download")
def download_attachment(
    conv_id: str,
    att_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    is_participant = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == current_user.id,
        )
        .first()
    )
    if not is_participant:
        raise HTTPException(403)

    att = db.query(MessageAttachment).filter(
        MessageAttachment.id == att_id,
        MessageAttachment.message_id.in_(
            db.query(Message.id).filter(Message.conversation_id == conv_id)
        ),
    ).first()
    if not att:
        raise HTTPException(404)

    url = storage.get_chat_file_url(att.file_key, att.filename)
    return RedirectResponse(url, status_code=302)


@router.post("/direct/{target_user_id}")
def start_direct(
    target_user_id: str,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    target = db.query(User).filter(User.id == target_user_id, User.is_active == True).first()
    if not target:
        raise HTTPException(404)

    conv = _get_or_create_direct(current_user.id, target.id, db)
    return RedirectResponse(f"/messaging/{conv.id}", status_code=302)


@router.post("/group/new")
def create_group(
    request: Request,
    name: str = Form(...),
    member_ids: list[str] = Form(default=[]),
    department_id: str = Form(""),
    zone_id: str = Form(""),
    branch_id: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    conv = Conversation(
        id=uuid.uuid4(),
        type=CONV_GROUP,
        name=name.strip(),
        department_id=department_id or None,
        zone_id=zone_id or None,
        branch_id=branch_id or None,
        created_by=current_user.id,
    )
    db.add(conv)
    db.flush()

    all_ids = set(member_ids) | {str(current_user.id)}
    for uid in all_ids:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid))

    db.commit()
    audit.log_action(
        "group_create", user=current_user, request=request,
        resource_type="conversation", resource_id=conv.id, resource_name=conv.name,
        details=f"members={len(all_ids)}",
    )
    return RedirectResponse(f"/messaging/{conv.id}", status_code=302)


@router.post("/{conv_id}/members")
def add_member(
    conv_id: str,
    request: Request,
    user_id: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    conv = db.query(Conversation).filter(
        Conversation.id == conv_id,
        Conversation.type == CONV_GROUP,
    ).first()
    if not conv:
        raise HTTPException(404)

    is_participant = any(str(p.user_id) == str(current_user.id) for p in conv.participants)
    if not is_participant:
        raise HTTPException(403)

    already = any(str(p.user_id) == user_id for p in conv.participants)
    if already:
        return RedirectResponse(f"/messaging/{conv_id}", status_code=302)

    target = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not target:
        raise HTTPException(404)

    db.add(ConversationParticipant(conversation_id=conv.id, user_id=target.id))
    db.commit()
    audit.log_action(
        "group_member_add", user=current_user, request=request,
        resource_type="conversation", resource_id=conv_id, resource_name=conv.name,
        details=f"added={target.email}",
    )
    return RedirectResponse(f"/messaging/{conv_id}", status_code=302)


@router.post("/{conv_id}/members/remove")
def remove_member(
    conv_id: str,
    request: Request,
    user_id: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    conv = db.query(Conversation).filter(
        Conversation.id == conv_id,
        Conversation.type == CONV_GROUP,
    ).first()
    if not conv:
        raise HTTPException(404)

    is_creator = str(conv.created_by) == str(current_user.id)
    is_superadmin = current_user.role == ROLE_SUPERADMIN
    removing_self = user_id == str(current_user.id)
    if not (is_creator or is_superadmin or removing_self):
        raise HTTPException(403)

    removed_user = db.query(User).filter(User.id == user_id).first()
    participant = db.query(ConversationParticipant).filter(
        ConversationParticipant.conversation_id == conv_id,
        ConversationParticipant.user_id == user_id,
    ).first()
    if participant:
        db.delete(participant)
        db.commit()

    action = "group_leave" if removing_self else "group_member_remove"
    audit.log_action(
        action, user=current_user, request=request,
        resource_type="conversation", resource_id=conv_id, resource_name=conv.name,
        details=f"user={removed_user.email if removed_user else user_id}",
    )

    if removing_self:
        return RedirectResponse("/messaging/", status_code=302)
    return RedirectResponse(f"/messaging/{conv_id}", status_code=302)
