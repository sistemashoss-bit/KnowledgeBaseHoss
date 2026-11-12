"""Enviar mensajes (texto, archivos, ubicación, stickers), pizarras y adjuntos."""

import mimetypes
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.database import get_db
from app import audit, storage, images
from app.messaging import stickers
from app.models import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageAttachment,
    User,
    Whiteboard,
    WhiteboardShare,
    CONV_GROUP,
)
from app.templating import templates, whiteboard_message
from app.messaging.helpers import (
    _FEED_LOAD,
    _after_new_message,
    _feed_context,
    _mark_read,
    _message_in_conv,
    _other_participant_ids,
    _require_participant,
    _safe_filename,
    osm_location_url,
)

router = APIRouter()


@router.post("/{conv_id}/send", response_class=HTMLResponse)
def send_message(
    conv_id: str,
    request: Request,
    content: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
    location: str = Form(default=""),
    sticker: str = Form(default=""),
    reply_to: str = Form(default=""),
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
    # Respuesta: el original debe ser de este mismo chat.
    reply_to_id = _message_in_conv(conv_id, reply_to, db).id if reply_to else None

    msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv_id,
        sender_id=current_user.id,
        content=content,
        reply_to_id=reply_to_id,
    )
    db.add(msg)
    db.flush()

    content_types = []
    for f in valid_files:
        # Algunos navegadores mandan .m4a/.opus sin tipo: se deduce por extensión.
        ctype = f.content_type
        if not ctype or ctype == "application/octet-stream":
            ctype = mimetypes.guess_type(f.filename)[0] or "application/octet-stream"
        data, filename, ctype = images.optimize_image(f.file.read(), f.filename, ctype)
        safe = _safe_filename(filename)
        key = f"chats/{conv_id}/{msg.id}/{uuid.uuid4()}_{safe}"
        content_types.append(ctype)
        storage.upload_chat_file(key, data, ctype)
        db.add(MessageAttachment(
            id=uuid.uuid4(),
            message_id=msg.id,
            uploaded_by=current_user.id,
            filename=filename,
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
    _after_new_message(conv_id, msg.id, current_user, db, preview, content)

    if valid_files:
        audit.log_action(
            "chat_attachment", user=current_user, request=request,
            resource_type="conversation", resource_id=conv_id,
            details=f"files={len(valid_files)} names={','.join(f.filename for f in valid_files[:5])}",
        )
    # Se devuelve el mensaje ya dibujado: el formulario lo agrega al feed sin
    # esperar otro viaje a /poll (el poll que dispara el SSE llega vacío o, si
    # gana la carrera, el feed descarta el repetido).
    sent = db.query(Message).options(*_FEED_LOAD).filter(Message.id == msg.id).one()
    conv_type = db.query(Conversation.type).filter(Conversation.id == conv_id).scalar()
    return templates.TemplateResponse(
        request,
        "messaging/_messages.html",
        _feed_context([sent], current_user, db, conv_type == CONV_GROUP, conv_id),
    )


@router.post("/{conv_id}/whiteboard")
def create_chat_whiteboard(
    conv_id: str,
    request: Request,
    title: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Crea una pizarra desde el chat: es de quien la crea, se comparte (con
    edición) con los demás participantes y queda como mensaje con el acceso
    (filtro whiteboard_link de templating.py la dibuja como tarjeta)."""
    if not current_user:
        raise HTTPException(401)
    _require_participant(conv_id, current_user, db)
    title = " ".join(title.split())[:200]
    if not title:
        raise HTTPException(422, "El título es obligatorio")

    now = datetime.utcnow()
    board = Whiteboard(id=uuid.uuid4(), title=title, owner_id=current_user.id, created_at=now, updated_at=now)
    others = _other_participant_ids(conv_id, current_user.id, db)
    active = {
        row[0] for row in db.query(User.id).filter(User.id.in_(others), User.is_active.is_(True))
    } if others else set()
    board.shares = [WhiteboardShare(user_id=uid, can_edit=True, created_at=now) for uid in active]
    db.add(board)

    content = whiteboard_message(title, board.id)
    msg = Message(id=uuid.uuid4(), conversation_id=conv_id, sender_id=current_user.id, content=content)
    db.add(msg)
    db.flush()
    _mark_read(conv_id, current_user.id, db)
    db.commit()

    _after_new_message(conv_id, msg.id, current_user, db, f"🎨 Pizarra: {title}"[:120])
    audit.log_action(
        "whiteboard_create", user=current_user, request=request,
        resource_type="whiteboard", resource_id=board.id, resource_name=board.title,
        details=f"chat={conv_id} shares={len(active)}",
    )
    return {"ok": True, "url": f"/whiteboards/{board.id}"}


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
