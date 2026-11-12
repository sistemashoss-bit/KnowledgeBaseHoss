"""Páginas y fragmentos del chat: índice, conversación, poll, "ver anteriores", señales y stickers."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from jose import JWTError
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import decode_token, generate_csrf_token
from app.database import get_db
from app import mentions, valkey_client as vk
from app.messaging import realtime, stickers
from app.models import (
    Conversation,
    ConversationParticipant,
    Department,
    Message,
    CONV_DIRECT,
    CONV_GROUP,
    ASSISTANT_BOT_NAME,
)
from app.templating import templates
from app.messaging.helpers import (
    _FEED_LOAD,
    _can_create_group,
    _contact_avatars,
    _conv_display_name,
    _feed_context,
    _feed_page,
    _format_last_seen,
    _mark_read,
    _message_created_at,
    _require_participant,
    _user_conversations,
    _visible_users,
)

router = APIRouter()


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
            # Autocompletado de @menciones del compositor: en grupos los
            # participantes (MAX incluido); en directos solo MAX.
            "mention_people": [
                {"name": mentions.display_name(p.user), "email": "Asistente" if p.user.is_bot else p.user.email}
                for p in conv.participants if p.user and str(p.user_id) != str(current_user.id)
            ] if conv.type == CONV_GROUP else [{"name": ASSISTANT_BOT_NAME, "email": "Asistente"}],
            "mention_everyone": conv.type == CONV_GROUP,
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
            .options(*_FEED_LOAD)
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
