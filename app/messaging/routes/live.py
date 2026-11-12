"""Tiempo real: SSE de la conversación y llamadas (token LiveKit, timbre, cancelar)."""

import json
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from jose import JWTError
from livekit.api import AccessToken, VideoGrants
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.utils import decode_token
from app.database import get_db, SessionLocal
from app import valkey_client as vk
from app.messaging import realtime
from app.models import ConversationParticipant
from app.messaging.helpers import (
    _other_participant_ids,
)

router = APIRouter()


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
