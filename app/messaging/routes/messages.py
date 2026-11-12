"""Acciones sobre un mensaje: reacciones, editar, eliminar, fijar, reenviar y búsqueda."""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.database import get_db
from app import audit, reactions, storage
from app.messaging import realtime, stickers
from app.models import Message, MessageAttachment, MessageReaction, User
from app.templating import _msg_preview, _osm_location, templates
from app.messaging.helpers import (
    EDIT_WINDOW,
    MAX_FORWARD_TARGETS,
    MAX_PINNED,
    _SEARCH_LIMIT,
    _after_new_message,
    _get_or_create_direct,
    _mark_read,
    _message_in_conv,
    _messages_anyone,
    _render_message,
    _require_participant,
    _safe_filename,
    _visible_users,
)

logger = logging.getLogger("app.messaging.router")  # mismo nombre que antes de dividir el módulo
router = APIRouter()


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


def _editable(msg: Message) -> bool:
    """Solo texto propio (no sticker/ubicación, solo-adjuntos ni reenviado), dentro de EDIT_WINDOW."""
    return (
        not msg.deleted_at
        and not msg.forwarded
        and bool(msg.content)
        and not stickers.STICKER_URL_RE.fullmatch(msg.content)
        and not _osm_location(msg.content)
        and datetime.utcnow() - msg.created_at <= EDIT_WINDOW
    )


def _own_message(conv_id: str, msg_id: str, current_user, db: Session) -> Message:
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    msg = _message_in_conv(conv_id, msg_id, db)
    if msg.sender_id != current_user.id:
        raise HTTPException(403)
    return msg


@router.get("/{conv_id}/messages/{msg_id}", response_class=HTMLResponse)
def message_row(
    conv_id: str,
    msg_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Fila de un mensaje (la pide el cliente al recibir `event: msgupdate`)."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    return _render_message(request, _message_in_conv(conv_id, msg_id, db), current_user, db)


@router.post("/{conv_id}/messages/{msg_id}/edit", response_class=HTMLResponse)
def edit_message(
    conv_id: str,
    msg_id: str,
    request: Request,
    content: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    msg = _own_message(conv_id, msg_id, current_user, db)
    if not _editable(msg):
        raise HTTPException(409, "Este mensaje ya no se puede editar (solo durante 15 minutos).")
    content = content.strip()
    if not content:
        raise HTTPException(400)
    if content != msg.content:
        msg.content = content
        msg.edited_at = datetime.utcnow()
        db.commit()
        realtime.notify_message_update(conv_id, msg.id)
    return _render_message(request, msg, current_user, db)


@router.post("/{conv_id}/messages/{msg_id}/delete", response_class=HTMLResponse)
def delete_message(
    conv_id: str,
    msg_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Eliminar para todos: queda "Mensaje eliminado"; se borran texto, adjuntos
    (también de Wasabi) y reacciones."""
    msg = _own_message(conv_id, msg_id, current_user, db)
    if not msg.deleted_at:
        names = [a.filename for a in msg.attachments]
        for att in msg.attachments:
            try:
                storage.delete_chat_file(att.file_key)
            except Exception:
                logger.warning("delete_message: no se pudo borrar %s", att.file_key, exc_info=True)
        db.query(MessageAttachment).filter(MessageAttachment.message_id == msg.id).delete(synchronize_session=False)
        db.query(MessageReaction).filter(MessageReaction.message_id == msg.id).delete(synchronize_session=False)
        msg.content = ""
        msg.deleted_at = datetime.utcnow()
        msg.pinned_at = msg.pinned_by = None
        db.commit()
        db.refresh(msg)
        realtime.notify_message_update(conv_id, msg.id)
        audit.log_action(
            "chat_message_delete", user=current_user, request=request,
            resource_type="conversation", resource_id=conv_id,
            details=f"message={msg.id}" + (f" files={','.join(names[:5])}" if names else ""),
        )
    return _render_message(request, msg, current_user, db)


@router.get("/{conv_id}/pins", response_class=HTMLResponse)
def pinned_messages(
    conv_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Barra de fijados del chat (se vuelve a pedir con cada `msgupdate`)."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    pins = (
        db.query(Message)
        .options(joinedload(Message.sender))
        .filter(Message.conversation_id == conv_id, Message.pinned_at.isnot(None))
        .order_by(Message.pinned_at.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "messaging/_pins.html", {"pins": pins, "conv_id": conv_id, "current_user": current_user},
    )


@router.post("/{conv_id}/messages/{msg_id}/pin", response_class=HTMLResponse)
def pin_message(
    conv_id: str,
    msg_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Fija para todo el chat (cualquier participante). Con MAX_PINNED ya fijados
    se desfija el más antiguo."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    msg = _message_in_conv(conv_id, msg_id, db)
    if msg.deleted_at:
        raise HTTPException(400, "No se puede fijar un mensaje eliminado.")
    if not msg.pinned_at:
        pinned = (
            db.query(Message)
            .filter(Message.conversation_id == conv_id, Message.pinned_at.isnot(None))
            .order_by(Message.pinned_at.asc())
            .all()
        )
        for old in pinned[: max(0, len(pinned) - MAX_PINNED + 1)]:
            old.pinned_at = old.pinned_by = None
            realtime.notify_message_update(conv_id, old.id)
        msg.pinned_at = datetime.utcnow()
        msg.pinned_by = current_user.id
        db.commit()
        realtime.notify_message_update(conv_id, msg.id)
    return _render_message(request, msg, current_user, db)


@router.post("/{conv_id}/messages/{msg_id}/unpin", response_class=HTMLResponse)
def unpin_message(
    conv_id: str,
    msg_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    msg = _message_in_conv(conv_id, msg_id, db)
    if msg.pinned_at:
        msg.pinned_at = msg.pinned_by = None
        db.commit()
        realtime.notify_message_update(conv_id, msg.id)
    return _render_message(request, msg, current_user, db)


def _forward_target(target: str, current_user: User, db: Session) -> str:
    """"c:<conv_id>" (chat propio) o "u:<user_id>" (persona: abre el chat directo
    si aún no existe, con la misma regla que start_direct). Devuelve el conv_id."""
    kind, _, raw = target.partition(":")
    try:
        target_id = uuid.UUID(raw)
    except ValueError:
        raise HTTPException(400)
    if kind == "c":
        _require_participant(str(target_id), current_user, db)
        return str(target_id)
    if kind == "u":
        user = db.query(User).filter(User.id == target_id, User.is_active == True).first()
        if not user or user.id == current_user.id:
            raise HTTPException(404)
        if not _messages_anyone(current_user) and user.id not in {u.id for u in _visible_users(current_user, db)}:
            raise HTTPException(403)
        return str(_get_or_create_direct(current_user.id, user.id, db).id)
    raise HTTPException(400)


@router.post("/{conv_id}/messages/{msg_id}/forward")
def forward_message(
    conv_id: str,
    msg_id: str,
    request: Request,
    targets: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Reenvía una copia (marcada "Reenviado") a uno o varios chats o personas.
    Los adjuntos se copian en Wasabi con llave propia: el reenvío no depende del
    original (que puede eliminarse o purgarse)."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    src = _message_in_conv(conv_id, msg_id, db)
    if src.deleted_at:
        raise HTTPException(400, "No se puede reenviar un mensaje eliminado.")
    targets = list(dict.fromkeys(t for t in targets if t))
    if not targets or len(targets) > MAX_FORWARD_TARGETS:
        raise HTTPException(400, f"Elige de 1 a {MAX_FORWARD_TARGETS} chats.")
    conv_ids = list(dict.fromkeys(_forward_target(t, current_user, db) for t in targets))

    preview = "↪ " + _msg_preview(src)
    sent = []
    for target_conv in conv_ids:
        msg = Message(
            id=uuid.uuid4(), conversation_id=target_conv, sender_id=current_user.id,
            content=src.content, forwarded=True,
        )
        db.add(msg)
        db.flush()
        for att in src.attachments:
            key = f"chats/{target_conv}/{msg.id}/{uuid.uuid4()}_{_safe_filename(att.filename)}"
            storage.copy_chat_file(att.file_key, key)
            db.add(MessageAttachment(
                id=uuid.uuid4(), message_id=msg.id, uploaded_by=current_user.id, filename=att.filename,
                file_key=key, content_type=att.content_type, file_size=att.file_size,
            ))
        _mark_read(target_conv, current_user.id, db)
        db.commit()
        _after_new_message(target_conv, msg.id, current_user, db, preview)
        sent.append(target_conv)

    audit.log_action(
        "chat_message_forward", user=current_user, request=request,
        resource_type="conversation", resource_id=conv_id,
        details=f"message={src.id} to={','.join(sent)}",
    )
    return {"sent": len(sent), "conversations": sent}


@router.get("/{conv_id}/search", response_class=HTMLResponse)
def search_messages(
    conv_id: str,
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Buscar texto en el chat abierto (sin eliminados, stickers ni ubicaciones)."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    q = q.strip()[:100]
    results = []
    if len(q) >= 2:
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        results = (
            db.query(Message)
            .options(joinedload(Message.sender))
            .filter(
                Message.conversation_id == conv_id,
                Message.deleted_at.is_(None),
                Message.content.ilike(pattern, escape="\\"),
                ~Message.content.like("https://mssgs-stickers.com/files/%"),
                ~Message.content.like("https://www.openstreetmap.org/?mlat=%"),
            )
            .order_by(Message.created_at.desc())
            .limit(_SEARCH_LIMIT)
            .all()
        )
    return templates.TemplateResponse(
        request,
        "messaging/_search_results.html",
        {"q": q, "results": results, "limit": _SEARCH_LIMIT, "current_user": current_user},
    )
