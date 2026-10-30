"""Stickers del chat, de mssgs-stickers.com.

Licencia (https://mssgs-stickers.com/license): gratis, también comercial, con el
crédito "Stickers by mssgs-stickers.com" visible (va al pie del selector) y el del
autor en los stickers de la comunidad (viene en `credit`).

La API solo se llama desde aquí: la key queda en el .env y su límite (120
llamadas/min) es por key, compartido por todos, así que las respuestas se
cachean en Valkey. Las imágenes se enlazan directo de su CDN (la licencia lo
permite; son inmutables y con caché de un año).
"""
import logging
import re
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from app import valkey_client as vk
from app.config import settings

logger = logging.getLogger(__name__)

API = "https://mssgs-stickers.com/api/v1"
CREDIT = "Stickers by mssgs-stickers.com"
PAGE_SIZE = 60
_TTL = 6 * 3600

# Un mensaje-sticker es exactamente la URL de su animación (APNG).
STICKER_URL_RE = re.compile(r"https://mssgs-stickers\.com/files/sticker-[a-z0-9-]+-v\d+\.apng")


def enabled() -> bool:
    return bool(settings.mssgs_stickers_api_key)


def _api(path: str, **params) -> dict | None:
    """GET a la API con caché. None si no existe (404); 503 si la API falla."""
    if not enabled():
        raise HTTPException(404)
    query = urlencode(sorted((k, v) for k, v in params.items() if v not in (None, "")))
    cache_key = f"stickers:{path}?{query}"
    cached = vk.get_json(cache_key)
    if cached is not None:
        return cached or None
    try:
        r = httpx.get(
            f"{API}{path}", params=query or None, timeout=8.0,
            headers={"Authorization": f"Bearer {settings.mssgs_stickers_api_key}"},
        )
    except httpx.HTTPError:
        logger.warning("stickers: API sin respuesta (%s)", path, exc_info=True)
        raise HTTPException(503, "El servicio de stickers no responde.")
    if r.status_code == 404:
        vk.set_json(cache_key, {}, _TTL)
        return None
    if r.status_code != 200:
        logger.warning("stickers: API %s -> %s %s", path, r.status_code, r.text[:200])
        raise HTTPException(503, "El servicio de stickers no está disponible.")
    data = r.json()
    vk.set_json(cache_key, data, _TTL)
    return data


def _slim(s: dict) -> dict:
    return {
        "id": s["id"],
        "name": s.get("name") or "",
        "thumb": s["thumbnail"]["url"],
        "credit": s.get("credit") or CREDIT,
    }


def packs() -> list[dict]:
    data = _api("/packs") or {}
    return [
        {"slug": p["slug"], "name": p["name"], "cover": p["cover"]["url"]}
        for p in data.get("packs", [])
    ]


def search(q: str = "", pack: str = "", offset: int = 0) -> dict:
    data = _api("/stickers", q=q.strip()[:80], pack=pack, lang="es", limit=PAGE_SIZE, offset=max(offset, 0)) or {}
    return {
        "stickers": [_slim(s) for s in data.get("stickers", [])],
        "next_offset": offset + PAGE_SIZE if data.get("next") else None,
    }


def animation_url(sticker_id: str) -> str | None:
    """URL del APNG de un sticker, o None si no existe."""
    if not re.fullmatch(r"[a-z0-9-]{1,120}", sticker_id or ""):
        return None
    data = _api(f"/stickers/{sticker_id}")
    url = (((data or {}).get("sticker") or {}).get("animation") or {}).get("url", "")
    return url if STICKER_URL_RE.fullmatch(url) else None


def require_url(sticker_id: str) -> str:
    """animation_url o 400: para guardar un sticker como contenido (chat/comentarios)."""
    url = animation_url(sticker_id)
    if not url:
        raise HTTPException(400, "Sticker no válido.")
    return url


def is_sticker(content: str | None) -> bool:
    return bool(STICKER_URL_RE.fullmatch((content or "").strip()))
