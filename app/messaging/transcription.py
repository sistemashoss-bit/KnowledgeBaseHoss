"""Transcripción de audios del chat con Whisper (OpenRouter).

POST {OPENROUTER}/audio/transcriptions con JSON
`{"model", "input_audio": {"data": <base64>, "format": "webm"}}` → `{"text", "usage"}`
(https://openrouter.ai/openai/whisper-large-v3-turbo).

El texto queda en el adjunto (`transcript`) y lo ven todos los del chat. Cada
transcripción nueva cuesta 1 consulta del cupo diario de MAX de quien la pide
(botón "Transcribir" o MAX con la tool transcribe_audio); si ya estaba
transcrito no se cobra. Si falla, se devuelve la consulta.
"""
import base64
import logging
import os
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app import audit, storage
from app import valkey_client as vk
from app.assistant import limits
from app.config import settings
from app.messaging import realtime
from app.models import MessageAttachment, User

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
TIMEOUT_SECONDS = 120
_LOCK_TTL = TIMEOUT_SECONDS + 30

# Extensión → formato que espera la API (webm/ogg con opus de las notas de voz).
_FORMATS = {
    "webm": "webm", "ogg": "ogg", "oga": "ogg", "opus": "ogg", "m4a": "m4a", "mp4": "mp4",
    "mp3": "mp3", "mpeg": "mp3", "wav": "wav", "flac": "flac", "aac": "aac",
}


class TranscriptionError(Exception):
    """Motivo para mostrar tal cual a quien la pidió."""


def _audio_format(att: MessageAttachment) -> str | None:
    ext = os.path.splitext(att.filename or "")[1].lstrip(".").lower()
    if ext in _FORMATS:
        return _FORMATS[ext]
    sub = (att.content_type or "").split(";")[0].split("/")[-1].lower()
    return _FORMATS.get(sub.replace("x-", ""))


def _call_api(audio: bytes, fmt: str) -> str:
    resp = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json={
            "model": settings.transcription_model,
            "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt},
        },
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    return (resp.json().get("text") or "").strip()


def transcribe(db: Session, att: MessageAttachment, user: User, *, extra: bool = False) -> tuple[str, bool]:
    """(texto, nuevo). `extra`: lo pidió MAX dentro de una invocación ya contada
    (cobra solo el cupo diario, no el de ráfaga). Lanza TranscriptionError."""
    if att.transcribed_at:
        return att.transcript or "", False
    if not att.is_audio:
        raise TranscriptionError("Ese adjunto no es un audio.")
    fmt = _audio_format(att)
    if fmt is None:
        raise TranscriptionError("No reconozco el formato de ese audio.")
    if att.file_size and att.file_size > settings.transcription_max_mb * 1024 * 1024:
        raise TranscriptionError(f"El audio pesa más de {settings.transcription_max_mb} MB; no lo puedo transcribir.")

    lock = f"transcribe:{att.id}"
    if not vk.acquire_lock(lock, _LOCK_TTL):
        raise TranscriptionError("Ese audio ya se está transcribiendo; espera un momento.")
    try:
        exceeded = limits.charge_extra(user.id) if extra else limits.check_user(user.id)
        if exceeded:
            raise TranscriptionError(limits.limit_message(*exceeded))
        try:
            text = _call_api(storage.read_chat_file(att.file_key), fmt)
        except Exception:
            logger.exception("transcription failed for attachment %s", att.id)
            limits.refund(user.id)
            raise TranscriptionError("No pude transcribir el audio en este momento; intenta de nuevo en un rato.")

        att.transcript = text
        att.transcribed_at = datetime.utcnow()
        att.transcribed_by = user.id
        db.commit()
    finally:
        vk.release_lock(lock)

    conv_id = att.message.conversation_id
    realtime.notify_message_update(conv_id, att.message_id)
    audit.log_action(
        "chat_audio_transcribe", user=user, resource_type="conversation", resource_id=str(conv_id),
        details=f"attachment={att.id} via={'assistant' if extra else 'button'} chars={len(text)}",
    )
    return text, True
