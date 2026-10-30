import json
import mimetypes
import os
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from jose import JWTError
from livekit.api import AccessToken, VideoGrants
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import decode_token, generate_csrf_token
from app.database import get_db, SessionLocal
from app import audit, mentions, notification_bus, reactions, storage, valkey_client as vk
from app.messaging import realtime, stickers
from app.permissions import branch_user_ids, manager_scope
from app.models import (
    Conversation, ConversationParticipant, Department,
    Message, MessageAttachment, MessageReaction, User,
    ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_AUDITOR, CONV_DIRECT, CONV_GROUP,
)
from app.templating import templates

_ONLINE_THRESHOLD = 180  # seconds — "En línea" if seen within 3 minutes
_FEED_PAGE = 50  # mensajes por carga del feed; los anteriores con "Ver anteriores"


def _format_last_seen(iso_str: str | None) -> str | None:
    if not iso_str:
        return None
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
        if diff < _ONLINE_THRESHOLD:
            return "En línea"
        if diff < 3600:
            m = diff // 60
            return f"Visto hace {m} min"
        if diff < 86400:
            h = diff // 3600
            return f"Visto hace {h} h"
        d = diff // 86400
        return f"Visto hace {d} día{'s' if d > 1 else ''}"
    except Exception:
        return None


def osm_location_url(lat: float, lng: float) -> str:
    return f"https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map=17/{lat:.6f}/{lng:.6f}"


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


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

    # Batch-fetch last_seen for all DM other-users in one MGET
    dm_other_ids = []
    for conv in convs:
        if conv.type == CONV_DIRECT:
            other = next(
                (p.user for p in conv.participants if str(p.user_id) != str(user.id)), None
            )
            if other:
                dm_other_ids.append(other.id)
    lastseen_map = vk.get_last_seen_many(dm_other_ids) if dm_other_ids else {}

    # Último mensaje y no leídos de TODAS las conversaciones en dos consultas
    # (antes eran dos por conversación); ambas van por ix_messages_conversation_created.
    conv_ids = [c.id for c in convs]
    last_msgs: dict = {}
    unread_map: dict = {}
    if conv_ids:
        last_msgs = {
            m.conversation_id: m
            for m in db.query(Message)
            .filter(Message.conversation_id.in_(conv_ids))
            .distinct(Message.conversation_id)
            .order_by(Message.conversation_id, Message.created_at.desc())
        }
        unread_map = dict(
            db.query(Message.conversation_id, func.count(Message.id))
            .join(ConversationParticipant, and_(
                ConversationParticipant.conversation_id == Message.conversation_id,
                ConversationParticipant.user_id == user.id,
            ))
            .filter(
                Message.conversation_id.in_(conv_ids),
                Message.sender_id != user.id,
                or_(
                    ConversationParticipant.last_read_at.is_(None),
                    Message.created_at > ConversationParticipant.last_read_at,
                ),
            )
            .group_by(Message.conversation_id)
            .all()
        )

    result = []
    for conv in convs:
        last_msg = last_msgs.get(conv.id)
        unread = unread_map.get(conv.id, 0)

        other_avatar_key = None
        is_online = False
        last_seen_text = None
        if conv.type == CONV_DIRECT:
            other = next(
                (p.user for p in conv.participants if str(p.user_id) != str(user.id)), None
            )
            if other:
                other_avatar_key = other.avatar_key
                last_seen_text = _format_last_seen(lastseen_map.get(str(other.id)))
                is_online = last_seen_text == "En línea"

        result.append(
            {
                "conv": conv,
                "last_msg": last_msg,
                "unread": unread,
                "display_name": _conv_display_name(conv, user),
                "avatar": _conv_avatar(conv, user),
                "other_avatar_key": other_avatar_key,
                "last_at": last_msg.created_at if last_msg else conv.created_at,
                "is_online": is_online,
                "last_seen_text": last_seen_text,
            }
        )

    result.sort(key=lambda x: x["last_at"], reverse=True)
    return result


def _team_user_ids(user: User, db: Session) -> set:
    """IDs of users who share this user's department or branch, or fall in
    their management scope (en Comercial, la gente de sus sucursales)."""
    ids: set = set()

    if user.department_id:
        for u in db.query(User).filter(User.department_id == user.department_id, User.is_active == True).all():
            ids.add(u.id)

    if user.branch_id:
        for u in db.query(User).filter(User.branch_id == user.branch_id, User.is_active == True).all():
            ids.add(u.id)

    ids |= branch_user_ids(manager_scope(user, db), db)

    ids.discard(user.id)
    return ids


def _messages_anyone(user: User) -> bool:
    """Superadmin y auditor escriben a cualquiera y arman grupos con quien sea
    (el auditor necesita hablar con todas las áreas que revisa)."""
    return user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR)


def _contact_avatars(users: list[User]) -> dict[str, str]:
    """URL firmada del avatar de cada persona de las listas (DM, nuevo grupo,
    agregar miembros): una firma por persona aunque salga en las tres."""
    return {str(u.id): storage.get_avatar_signed_url(u.avatar_key) for u in users if u.avatar_key}


def _can_create_group(user: User) -> bool:
    return user.role in (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_AUDITOR)


def _visible_users(user: User, db: Session) -> list[User]:
    """Users this user can start a DM with (también los que puede sumar a un grupo)."""
    q = db.query(User).options(joinedload(User.department))
    if _messages_anyone(user):
        return q.filter(User.is_active == True, User.id != user.id).order_by(User.email).all()

    if user.role == ROLE_ADMIN:
        # Admins (with or without a zone) can only message their own team
        # plus other admins/superadmins — not the whole company.
        ids = _team_user_ids(user, db)
        for u in db.query(User).filter(
            User.role.in_((ROLE_ADMIN, ROLE_SUPERADMIN)), User.is_active == True
        ).all():
            ids.add(u.id)
        ids.discard(user.id)
        if not ids:
            return []
        return q.filter(User.id.in_(ids), User.is_active == True).order_by(User.email).all()

    ids = _team_user_ids(user, db)
    if not ids:
        return []
    return q.filter(User.id.in_(ids), User.is_active == True).order_by(User.email).all()


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


def _mark_read(conv_id, user_id, db: Session, *, force_publish: bool = False):
    p = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == user_id,
        )
        .first()
    )
    if p:
        # Se llama en cada poll del chat abierto: solo se avisa a
        # notificationservice cuando de verdad había algo sin leer. Al abrir el
        # chat (force_publish) se avisa siempre, para corregir un contador que
        # se haya quedado desfasado (p. ej. un `read` perdido).
        unread = db.query(Message.id).filter(
            Message.conversation_id == conv_id,
            Message.sender_id != user_id,
        )
        if p.last_read_at:
            unread = unread.filter(Message.created_at > p.last_read_at)
        had_unread = unread.first() is not None
        # Sin nada nuevo no hace falta escribir: los no leídos se cuentan desde
        # last_read_at, y el poll del chat abierto pasaría por aquí cada vez.
        if had_unread or force_publish:
            p.last_read_at = datetime.utcnow()
            db.commit()
            notification_bus.publish_read(conv_id, user_id)


def _feed_page(conv_id, db: Session, *, before_id: str | None = None) -> tuple[list[Message], bool]:
    """Los _FEED_PAGE mensajes más recientes (o los anteriores a `before_id`),
    en orden cronológico, y si quedan más viejos por cargar."""
    q = (
        db.query(Message)
        .options(joinedload(Message.sender), joinedload(Message.attachments))
        .filter(Message.conversation_id == conv_id)
    )
    if before_id:
        pivot = _message_created_at(conv_id, before_id, db)
        if pivot is None:
            return [], False
        q = q.filter(Message.created_at < pivot)
    rows = q.order_by(Message.created_at.desc()).limit(_FEED_PAGE + 1).all()
    has_more = len(rows) > _FEED_PAGE
    return list(reversed(rows[:_FEED_PAGE])), has_more


def _message_created_at(conv_id, msg_id: str, db: Session):
    try:
        uuid.UUID(msg_id)
    except ValueError:
        raise HTTPException(400)
    return (
        db.query(Message.created_at)
        .filter(Message.id == msg_id, Message.conversation_id == conv_id)
        .scalar()
    )


def _require_participant(conv_id: str, user: User, db: Session) -> None:
    try:
        uuid.UUID(conv_id)
    except ValueError:
        raise HTTPException(404)
    exists = (
        db.query(ConversationParticipant.user_id)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == user.id,
        )
        .first()
    )
    if not exists:
        raise HTTPException(403)


def _group_members(conv_id, db: Session) -> list[User]:
    return (
        db.query(User)
        .join(ConversationParticipant, ConversationParticipant.user_id == User.id)
        .filter(ConversationParticipant.conversation_id == conv_id)
        .all()
    )


def _feed_context(
    messages: list[Message], current_user: User, db: Session, is_group: bool,
    conv_id=None, members: list[User] | None = None,
) -> dict:
    """Contexto de _messages.html. En grupos incluye los nombres a resaltar como
    mención (`members` si ya están cargados; si no, una consulta, y solo si
    algún mensaje trae "@")."""
    names = []
    if is_group and any("@" in (m.content or "") for m in messages):
        names = [mentions.display_name(u) for u in (members if members is not None else _group_members(conv_id, db))]
    return {
        "messages": messages,
        "current_user": current_user,
        "is_group": is_group,
        "reactions": reactions.summarize(db, MessageReaction, [m.id for m in messages], current_user.id),
        "mention_names": names,
        "mention_me": mentions.display_name(current_user),
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def messaging_index(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    conv_list = _user_conversations(current_user, db)
    contacts = _visible_users(current_user, db)
    departments = db.query(Department).order_by(Department.name).all()

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
            "contact_avatars": _contact_avatars(contacts),
            "can_create_group": _can_create_group(current_user),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.get("/signals")
async def user_signals(request: Request):
    """Per-user SSE stream for call rings and other push signals.

    Registered before GET /{conv_id} so it isn't captured by that catch-all.
    Authenticated from the cookie directly (like conversation_events) so no DB
    connection is held for the lifetime of the stream.
    """
    token = request.cookies.get("access_token")
    user_id = None
    if token:
        try:
            user_id = decode_token(token).get("sub")
        except JWTError:
            user_id = None
    if not user_id:
        raise HTTPException(401)

    return StreamingResponse(
        realtime.user_event_stream(user_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/stickers/packs")
def sticker_packs(current_user=Depends(get_current_user)):
    """Packs para el selector (registrado antes de GET /{conv_id})."""
    if not current_user:
        raise HTTPException(401)
    return {"packs": stickers.packs(), "credit": stickers.CREDIT}


@router.get("/stickers/search")
def sticker_search(q: str = "", pack: str = "", offset: int = 0, current_user=Depends(get_current_user)):
    if not current_user:
        raise HTTPException(401)
    return stickers.search(q, pack, offset)


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

    _mark_read(conv_id, current_user.id, db, force_publish=True)

    messages, has_more = _feed_page(conv_id, db)

    conv_list = _user_conversations(current_user, db)
    contacts = _visible_users(current_user, db)
    departments = db.query(Department).order_by(Department.name).all()

    other_last_seen = None
    if conv.type == CONV_DIRECT:
        other = next(
            (p.user for p in conv.participants if str(p.user_id) != str(current_user.id)), None
        )
        if other:
            other_last_seen = _format_last_seen(vk.get_last_seen(other.id))

    return templates.TemplateResponse(
        request,
        "messaging/index.html",
        {
            "current_user": current_user,
            "conv_list": conv_list,
            "active_conv": conv,
            "active_conv_name": _conv_display_name(conv, current_user),
            "other_last_seen": other_last_seen,
            **_feed_context(
                messages, current_user, db, conv.type == CONV_GROUP,
                members=[p.user for p in conv.participants if p.user],
            ),
            "has_more": has_more,
            # Autocompletado de @menciones del compositor (solo grupos).
            "mention_people": [
                {"name": mentions.display_name(p.user), "email": p.user.email}
                for p in conv.participants if p.user and str(p.user_id) != str(current_user.id)
            ] if conv.type == CONV_GROUP else [],
            "contacts": contacts,
            "departments": departments,
            "contact_avatars": _contact_avatars(contacts),
            "can_create_group": _can_create_group(current_user),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.get("/{conv_id}/poll", response_class=HTMLResponse)
def poll_messages(
    conv_id: str,
    request: Request,
    after: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """HTMX polling endpoint — solo los mensajes posteriores a `after` (el último
    que ya tiene el cliente), que se agregan al final del feed. Sin `after` (chat
    que estaba vacío) devuelve la última página."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)

    if after:
        pivot = _message_created_at(conv_id, after, db)
        if pivot is None:
            # El último que tenía el cliente ya no existe (purga): que recargue entero.
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        messages = (
            db.query(Message)
            .options(joinedload(Message.sender), joinedload(Message.attachments))
            .filter(Message.conversation_id == conv_id, Message.created_at > pivot)
            .order_by(Message.created_at.asc())
            .limit(200)
            .all()
        )
    else:
        messages, _ = _feed_page(conv_id, db)
    if not messages:
        return HTMLResponse("")
    _mark_read(conv_id, current_user.id, db)

    conv_type = db.query(Conversation.type).filter(Conversation.id == conv_id).scalar()
    return templates.TemplateResponse(
        request,
        "messaging/_messages.html",
        _feed_context(messages, current_user, db, conv_type == CONV_GROUP, conv_id),
    )


@router.get("/{conv_id}/older", response_class=HTMLResponse)
def older_messages(
    conv_id: str,
    request: Request,
    before: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """"Ver anteriores": la página previa a `before`, que reemplaza al botón."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)

    messages, has_more = _feed_page(conv_id, db, before_id=before)
    conv_type = db.query(Conversation.type).filter(Conversation.id == conv_id).scalar()
    return templates.TemplateResponse(
        request,
        "messaging/_older.html",
        {**_feed_context(messages, current_user, db, conv_type == CONV_GROUP, conv_id), "has_more": has_more},
    )


def _message_in_conv(conv_id: str, msg_id: str, db: Session) -> Message:
    try:
        uuid.UUID(msg_id)
    except ValueError:
        raise HTTPException(404)
    msg = db.query(Message).filter(Message.id == msg_id, Message.conversation_id == conv_id).first()
    if not msg:
        raise HTTPException(404)
    return msg


def _render_message_reactions(request: Request, msg: Message, current_user: User, db: Session):
    summary = reactions.summarize(db, MessageReaction, [msg.id], current_user.id)
    return templates.TemplateResponse(
        request,
        "_reactions.html",
        {
            "rx_id": msg.id,
            "rx_list": summary.get(msg.id, []),
            "rx_url": f"/messaging/{msg.conversation_id}/messages/{msg.id}/reactions",
            "rx_class": "justify-end" if msg.sender_id == current_user.id else "",
        },
    )


@router.get("/{conv_id}/messages/{msg_id}/reactions", response_class=HTMLResponse)
def message_reactions(
    conv_id: str,
    msg_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Bloque de reacciones de un mensaje (lo pide el cliente al recibir `event: reaction`)."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    return _render_message_reactions(request, _message_in_conv(conv_id, msg_id, db), current_user, db)


@router.post("/{conv_id}/messages/{msg_id}/reactions", response_class=HTMLResponse)
def toggle_message_reaction(
    conv_id: str,
    msg_id: str,
    request: Request,
    emoji: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    msg = _message_in_conv(conv_id, msg_id, db)
    reactions.toggle(db, MessageReaction, msg.id, current_user.id, emoji)
    # Solo el bloque de ese mensaje en los demás clientes: sin refrescar el feed
    # ni tocar contadores de no leídos.
    realtime.notify_reaction(realtime.channel(conv_id), msg.id)
    return _render_message_reactions(request, msg, current_user, db)


@router.get("/{conv_id}/events")
async def conversation_events(conv_id: str, request: Request):
    """SSE stream: pushes a `message` event when the conversation gets a new message.

    Authenticated from the cookie directly (not via Depends(get_db)) so no DB
    connection is held for the lifetime of the stream.
    """
    token = request.cookies.get("access_token")
    user_id = None
    if token:
        try:
            user_id = decode_token(token).get("sub")
        except JWTError:
            user_id = None
    if not user_id:
        raise HTTPException(401)

    db = SessionLocal()
    try:
        is_participant = (
            db.query(ConversationParticipant)
            .filter(
                ConversationParticipant.conversation_id == conv_id,
                ConversationParticipant.user_id == user_id,
            )
            .first()
            is not None
        )
    finally:
        db.close()
    if not is_participant:
        raise HTTPException(403)

    return StreamingResponse(
        realtime.event_stream(conv_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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


# ── Call signalling (ring / cancel) ───────────────────────────────────────────

def _other_participant_ids(conv_id: str, exclude_user_id, db: Session) -> list:
    rows = (
        db.query(ConversationParticipant.user_id)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id != exclude_user_id,
        )
        .all()
    )
    return [r[0] for r in rows]


@router.post("/{conv_id}/call/ring")
def call_ring(
    conv_id: str,
    video: bool = Form(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Notify the other participants that the caller started a call."""
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

    payload = json.dumps({
        "type": "call",
        "conv_id": str(conv_id),
        "room": f"conv-{conv_id}",
        "caller_id": str(current_user.id),
        "caller_name": current_user.name or current_user.email,
        "video": bool(video),
        "call_id": str(uuid.uuid4()),
        "ts": datetime.utcnow().isoformat(),
    })
    for uid in _other_participant_ids(conv_id, current_user.id, db):
        vk.publish(realtime.user_channel(uid), payload)
    return JSONResponse({"ok": True})


@router.post("/{conv_id}/call/cancel")
def call_cancel(
    conv_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Retract the ring on the callee side when the caller hangs up unanswered."""
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

    payload = json.dumps({"type": "call_cancel", "conv_id": str(conv_id)})
    for uid in _other_participant_ids(conv_id, current_user.id, db):
        vk.publish(realtime.user_channel(uid), payload)
    return JSONResponse({"ok": True})


@router.post("/{conv_id}/send", response_class=HTMLResponse)
def send_message(
    conv_id: str,
    request: Request,
    content: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    location: str = Form(default=""),
    sticker: str = Form(default=""),
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

    storage.check_upload_sizes(*files)

    content = content.strip()
    valid_files = [f for f in files if f.filename]
    if location:
        # Ubicación compartida una sola vez ("lat,lng" del navegador): el mensaje es
        # solo el enlace de OSM, que la plantilla dibuja como mapa (filtro osm_location).
        try:
            lat, lng = (float(v) for v in location.split(","))
        except ValueError:
            raise HTTPException(400)
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise HTTPException(400)
        content = osm_location_url(lat, lng)
        valid_files = []
    elif sticker:
        # El mensaje es la URL del APNG (la plantilla lo dibuja con sticker_url).
        content = stickers.animation_url(sticker)
        if not content:
            raise HTTPException(400)
        valid_files = []
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

    content_types = []
    for f in valid_files:
        data = f.file.read()
        safe = _safe_filename(f.filename)
        key = f"chats/{conv_id}/{msg.id}/{uuid.uuid4()}_{safe}"
        # Algunos navegadores mandan .m4a/.opus sin tipo: se deduce por extensión.
        ctype = f.content_type
        if not ctype or ctype == "application/octet-stream":
            ctype = mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
        content_types.append(ctype)
        storage.upload_chat_file(key, data, ctype)
        db.add(MessageAttachment(
            id=uuid.uuid4(),
            message_id=msg.id,
            uploaded_by=current_user.id,
            filename=f.filename,
            file_key=key,
            content_type=ctype,
            file_size=len(data),
        ))

    _mark_read(conv_id, current_user.id, db)
    db.commit()

    # El mensaje va a notificationservice ANTES del fan-out en tiempo real: si el
    # destinatario tiene el chat abierto, su refresco publica `read` y debe
    # llegar después del `message`, o el contador queda en 1 para siempre.
    if location:
        preview = "📍 Ubicación"
    elif sticker:
        preview = "Sticker"
    elif content:
        preview = content[:120]
    elif all(ct.startswith("audio/") for ct in content_types):
        preview = "🎤 Nota de voz"
    else:
        preview = "Archivo adjunto"
    recipient_ids = _other_participant_ids(conv_id, current_user.id, db)
    conv = db.query(Conversation.type, Conversation.name).filter(Conversation.id == conv_id).first()
    notification_bus.publish_message(
        conv_id,
        (conv.name or "Grupo sin nombre") if conv.type == CONV_GROUP else None,
        current_user,
        recipient_ids,
        msg.id,
        preview,
        f"/messaging/{conv_id}",
    )

    # Menciones (solo grupos): a quien se mencionó le llega una notificación
    # `chat_mention` en vez del simple refresco del badge.
    mentioned: set = set()
    if conv.type == CONV_GROUP and "@" in content:
        others = [u for u in _group_members(conv_id, db) if u.id != current_user.id]
        mentioned, everyone = mentions.find(content, others)
        group = conv.name or "Grupo sin nombre"
        actor = mentions.display_name(current_user)
        url = f"/messaging/{conv_id}"
        if everyone:
            mentioned = {u.id for u in others}
            realtime.notify_users(mentioned, f"{actor} mencionó a todos en «{group}»", preview, url, "chat_mention")
        elif mentioned:
            realtime.notify_users(mentioned, f"{actor} te mencionó en «{group}»", preview, url, "chat_mention")

    # Real-time fan-out to every open client in this conversation.
    vk.publish(realtime.channel(conv_id))
    # Poke each recipient's Mensajes badge (SSE refresh trigger).
    for uid in recipient_ids:
        if uid not in mentioned:
            realtime.notify_user(uid)

    if valid_files:
        audit.log_action(
            "chat_attachment", user=current_user, request=request,
            resource_type="conversation", resource_id=conv_id,
            details=f"files={len(valid_files)} names={','.join(f.filename for f in valid_files[:5])}",
        )
    return HTMLResponse("", headers={"HX-Trigger": "refreshFeed"})


@router.get("/{conv_id}/files", response_class=HTMLResponse)
def conversation_files(
    conv_id: str,
    request: Request,
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

    attachments = (
        db.query(MessageAttachment)
        .options(joinedload(MessageAttachment.uploader))
        .join(Message, MessageAttachment.message_id == Message.id)
        .filter(Message.conversation_id == conv_id)
        .order_by(MessageAttachment.created_at.desc())
        .all()
    )
    media = [a for a in attachments if a.content_type.startswith("image/")]
    docs = [a for a in attachments if not a.content_type.startswith("image/")]

    return templates.TemplateResponse(
        request,
        "messaging/_files_panel.html",
        {"conv_id": conv_id, "media": media, "docs": docs},
    )


@router.get("/{conv_id}/attachments/{att_id}/download")
def download_attachment(
    conv_id: str,
    att_id: str,
    inline: bool = False,
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

    url = storage.get_chat_file_url(att.file_key, att.filename, inline=inline)
    # La URL firmada se reutiliza 50 min y siempre vale ≥ 10 min más (storage._stable_url):
    # cachear la redirección 10 min nunca lleva a una firma vencida, y la imagen
    # queda en la caché del navegador bajo esa misma URL.
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "private, max-age=600"})


@router.post("/direct/{target_user_id}")
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

    if not _messages_anyone(current_user):
        allowed_ids = {u.id for u in _visible_users(current_user, db)}
        if target.id not in allowed_ids:
            raise HTTPException(403)

    conv = _get_or_create_direct(current_user.id, target.id, db)
    return RedirectResponse(f"/messaging/{conv.id}", status_code=302)


@router.post("/group/new")
def create_group(
    request: Request,
    name: str = Form(...),
    member_ids: list[str] = Form(default=[]),
    department_id: str = Form(""),
    branch_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not _can_create_group(current_user):
        raise HTTPException(403)

    conv = Conversation(
        id=uuid.uuid4(),
        type=CONV_GROUP,
        name=name.strip(),
        department_id=department_id or None,
        branch_id=branch_id or None,
        created_by=current_user.id,
    )
    db.add(conv)
    db.flush()

    all_ids = set(member_ids) | {str(current_user.id)}
    if not _messages_anyone(current_user):
        allowed_ids = {str(u.id) for u in _visible_users(current_user, db)} | {str(current_user.id)}
        all_ids &= allowed_ids
    for uid in all_ids:
        db.add(ConversationParticipant(conversation_id=conv.id, user_id=uid))

    db.commit()
    audit.log_action(
        "group_create", user=current_user, request=request,
        resource_type="conversation", resource_id=conv.id, resource_name=conv.name,
        details=f"members={len(all_ids)}",
    )
    return RedirectResponse(f"/messaging/{conv.id}", status_code=302)


@router.post("/{conv_id}/edit")
def edit_group(
    conv_id: str,
    request: Request,
    name: str = Form(...),
    photo: UploadFile | None = File(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    conv = db.query(Conversation).filter(
        Conversation.id == conv_id,
        Conversation.type == CONV_GROUP,
    ).first()
    if not conv:
        raise HTTPException(404)

    is_creator = str(conv.created_by) == str(current_user.id)
    is_superadmin = current_user.role == ROLE_SUPERADMIN
    if not (is_creator or is_superadmin):
        raise HTTPException(403)

    storage.check_upload_sizes(photo)

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre no puede estar vacío")
    conv.name = name

    if photo is not None and photo.filename:
        if not (photo.content_type or "").startswith("image/"):
            raise HTTPException(400, "La foto debe ser una imagen")
        content = photo.file.read()
        old_key = conv.avatar_key
        ext = os.path.splitext(_safe_filename(photo.filename))[1] or ".jpg"
        new_key = f"group-avatars/{conv.id}/{uuid.uuid4()}{ext}"
        storage.upload_chat_file(new_key, content, photo.content_type)
        conv.avatar_key = new_key
        if old_key:
            try:
                storage.delete_chat_file(old_key)
            except Exception:
                pass

    db.commit()
    audit.log_action(
        "group_edit", user=current_user, request=request,
        resource_type="conversation", resource_id=conv_id, resource_name=conv.name,
    )
    return RedirectResponse(f"/messaging/{conv_id}", status_code=302)


@router.post("/{conv_id}/members")
def add_member(
    conv_id: str,
    request: Request,
    user_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

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

    if not _messages_anyone(current_user):
        allowed_ids = {u.id for u in _visible_users(current_user, db)}
        if target.id not in allowed_ids:
            raise HTTPException(403)

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
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

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


@router.post("/{conv_id}/delete")
def delete_conversation(
    conv_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete a direct chat for both parties: messages, attachment rows and files."""
    if not current_user:
        raise HTTPException(401)

    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    if not conv:
        raise HTTPException(404)
    if conv.type != CONV_DIRECT:
        raise HTTPException(400, "Solo se pueden eliminar chats directos")

    is_participant = (
        db.query(ConversationParticipant)
        .filter(
            ConversationParticipant.conversation_id == conv_id,
            ConversationParticipant.user_id == current_user.id,
        )
        .first()
        is not None
    )
    if not is_participant and current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    # Delete attachment files from object storage first — the DB cascade never
    # touches storage, so this is the only chance to grab their keys.
    keys = (
        db.query(MessageAttachment.file_key)
        .join(Message, MessageAttachment.message_id == Message.id)
        .filter(Message.conversation_id == conv_id)
        .all()
    )
    for (key,) in keys:
        try:
            storage.delete_chat_file(key)
        except Exception:
            pass

    # Explicit ordered delete so it works regardless of DB-level cascade state.
    msg_ids = [m_id for (m_id,) in db.query(Message.id).filter(Message.conversation_id == conv_id).all()]
    if msg_ids:
        db.query(MessageAttachment).filter(MessageAttachment.message_id.in_(msg_ids)).delete(synchronize_session=False)
        db.query(Message).filter(Message.id.in_(msg_ids)).delete(synchronize_session=False)
    db.query(ConversationParticipant).filter(ConversationParticipant.conversation_id == conv_id).delete(synchronize_session=False)
    db.query(Conversation).filter(Conversation.id == conv_id).delete(synchronize_session=False)
    db.commit()

    # Wake up the other party's open view so it stops polling a dead chat.
    vk.publish(realtime.channel(conv_id), "deleted")
    notification_bus.publish_deleted(conv_id)

    audit.log_action(
        "conversation_delete", user=current_user, request=request,
        resource_type="conversation", resource_id=conv_id,
        details=f"messages={len(msg_ids)} files={len(keys)}",
    )
    return RedirectResponse("/messaging/", status_code=302)
