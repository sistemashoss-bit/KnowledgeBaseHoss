"""Crear y administrar conversaciones: directos, grupos, miembros, eliminar."""

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app import audit, notification_bus, storage, valkey_client as vk, images
from app.messaging import realtime
from app.assistant import bot as assistant_bot
from app.models import (
    Conversation,
    ConversationParticipant,
    Message,
    MessageAttachment,
    User,
    ROLE_SUPERADMIN,
    CONV_DIRECT,
    CONV_GROUP,
)
from app.messaging.helpers import (
    _can_create_group,
    _get_or_create_direct,
    _messages_anyone,
    _safe_filename,
    _visible_users,
)

router = APIRouter()


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
    # MAX está en todos los grupos para que se le pueda invocar con @MAX.
    assistant_bot.add_to_conversation(conv.id, db)

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
        content, filename, ctype = images.optimize_image(photo.file.read(), photo.filename, photo.content_type)
        old_key = conv.avatar_key
        ext = os.path.splitext(_safe_filename(filename))[1] or ".jpg"
        new_key = f"group-avatars/{conv.id}/{uuid.uuid4()}{ext}"
        storage.upload_chat_file(new_key, content, ctype)
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
    if assistant_bot.is_bot_id(user_id):
        raise HTTPException(403, "MAX está en todos los grupos y no se puede quitar")

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
