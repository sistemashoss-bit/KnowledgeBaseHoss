"""Imágenes que genera MAX (OpenRouter Image API).

POST {OPENROUTER}/images con JSON `{"model", "prompt", "aspect_ratio", "n": 1}` →
`{"data": [{"b64_json", "media_type"}], "usage"}`
(https://openrouter.ai/docs/guides/overview/multimodal/image-generation).
Las generaciones que fallan no se cobran (ni en OpenRouter ni en el cupo).
"""
import base64
import uuid

import httpx

from app import storage
from app.config import settings

API_URL = "https://openrouter.ai/api/v1/images"
TIMEOUT_SECONDS = 90
MAX_BYTES = 15 * 1024 * 1024
ASPECT_RATIOS = ["1:1", "4:3", "3:4", "16:9", "9:16"]
# Solo raster: un SVG puede traer scripts y se sirve "inline".
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class ImageError(Exception):
    pass


def generate(prompt: str, aspect_ratio: str) -> tuple[bytes, str]:
    """(bytes, media_type) de una imagen. Lanza ImageError o errores de red."""
    resp = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json={"model": settings.image_model, "prompt": prompt, "aspect_ratio": aspect_ratio, "n": 1},
        timeout=TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    items = resp.json().get("data") or []
    if not items or not items[0].get("b64_json"):
        raise ImageError("respuesta sin imagen")
    media_type = (items[0].get("media_type") or "image/png").split(";")[0].strip().lower()
    if media_type not in _EXTENSIONS:
        raise ImageError(f"formato no soportado: {media_type}")
    data = base64.b64decode(items[0]["b64_json"])
    if not data or len(data) > MAX_BYTES:
        raise ImageError("imagen vacía o demasiado grande")
    return data, media_type


def store(conv_id, data: bytes, media_type: str, n: int) -> dict:
    """Sube la imagen al bucket de chats; el adjunto se crea al publicar la
    respuesta de MAX (ahí ya existe el mensaje)."""
    filename = f"imagen-max-{n}.{_EXTENSIONS[media_type]}"
    key = f"chats/{conv_id}/max/{uuid.uuid4()}_{filename}"
    storage.upload_chat_file(key, data, media_type)
    return {"filename": filename, "key": key, "content_type": media_type, "size": len(data)}
