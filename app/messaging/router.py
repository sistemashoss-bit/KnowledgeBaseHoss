import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    Branch, Conversation, ConversationParticipant, Department,
    Message, User, UserZone, Zone,
    ROLE_SUPERADMIN, CONV_DIRECT, CONV_GROUP,
)
from app.templating import templates

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

        result.append(
            {
                "conv": conv,
                "last_msg": last_msg,
                "unread": unread,
                "display_name": _conv_display_name(conv, user),
                "avatar": _conv_avatar(conv, user),
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
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return HTMLResponse(headers={"HX-Redirect": "/auth/login"})

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
        return HTMLResponse(headers={"HX-Redirect": "/auth/login"})

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
        .options(joinedload(Message.sender))
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
        .options(joinedload(Message.sender))
        .filter(Message.conversation_id == conv_id)
        .order_by(Message.created_at.asc())
        .limit(200)
        .all()
    )
    _mark_read(conv_id, current_user.id, db)

    return templates.TemplateResponse(
        request,
        "messaging/_feed.html",
        {"messages": messages, "current_user": current_user},
    )


@router.post("/{conv_id}/send", response_class=HTMLResponse)
def send_message(
    conv_id: str,
    content: str = Form(...),
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
    if not content:
        raise HTTPException(400)

    msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_id=current_user.id,
        content=content,
    )
    db.add(msg)
    _mark_read(conv_id, current_user.id, db)  # also marks as read
    db.commit()

    # Trigger feed refresh via HTMX event
    return HTMLResponse("", headers={"HX-Trigger": "refreshFeed"})


@router.post("/direct/{target_user_id}", response_class=HTMLResponse)
def start_direct(
    target_user_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    target = db.query(User).filter(User.id == target_user_id, User.is_active == True).first()
    if not target:
        raise HTTPException(404)

    conv = _get_or_create_direct(current_user.id, target.id, db)
    return HTMLResponse(headers={"HX-Redirect": f"/messaging/{conv.id}"})


@router.post("/group/new", response_class=HTMLResponse)
def create_group(
    name: str = Form(...),
    member_ids: list[str] = Form(default=[]),
    department_id: str = Form(""),
    zone_id: str = Form(""),
    branch_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

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

    # Add creator + selected members
    all_ids = set(member_ids) | {str(current_user.id)}
    for uid in all_ids:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid))

    db.commit()
    return HTMLResponse(headers={"HX-Redirect": f"/messaging/{conv.id}"})
