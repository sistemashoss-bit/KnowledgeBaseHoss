"""Cliente mínimo de la API de Mux Video para Capacitación.

Flujo: `create_upload` da una URL de direct upload a la que el navegador sube
el archivo (no pasa por este servidor). Mux avisa por webhook
(`video.upload.asset_created`, `video.asset.ready`, `video.asset.errored`) y,
por si el webhook no llega (desarrollo, red), `refresh` consulta la API.
Con llave de firma configurada los videos son `signed` y se reproducen con un
JWT de corta duración (`playback_tokens`); sin ella, `public`.
"""
import base64
import hashlib
import hmac
import logging
import time

import httpx
from jose import jwt

from app.config import settings

log = logging.getLogger(__name__)

API = "https://api.mux.com"
TOKEN_TTL = 6 * 60 * 60  # segundos de validez del token de reproducción
WEBHOOK_TOLERANCE = 5 * 60


class MuxError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.mux_token_id and settings.mux_token_secret)


def signed_enabled() -> bool:
    return bool(settings.mux_signing_key_id and settings.mux_signing_key_private)


def _request(method: str, path: str, **kwargs) -> dict:
    if not enabled():
        raise MuxError("Mux no está configurado (MUX_TOKEN_ID / MUX_TOKEN_SECRET)")
    try:
        r = httpx.request(
            method, f"{API}{path}", auth=(settings.mux_token_id, settings.mux_token_secret), timeout=20, **kwargs,
        )
    except httpx.HTTPError as e:
        raise MuxError(f"No se pudo contactar a Mux: {e}") from e
    if r.status_code == 404 and method == "DELETE":
        return {}
    if r.status_code >= 400:
        log.warning("Mux %s %s -> %s %s", method, path, r.status_code, r.text[:500])
        raise MuxError(f"Mux respondió {r.status_code}")
    return r.json() if r.content else {}


def create_upload(passthrough: str) -> tuple[str, str, str]:
    """Crea un direct upload. Devuelve (upload_id, url, playback_policy)."""
    policy = "signed" if signed_enabled() else "public"
    data = _request("POST", "/video/v1/uploads", json={
        "cors_origin": settings.public_url.rstrip("/"),
        "new_asset_settings": {
            "playback_policies": [policy], "passthrough": passthrough, "video_quality": "basic",
            # Subtítulos automáticos (Whisper): de ahí salen transcripción y capítulos.
            "inputs": [{"generated_subtitles": [_captions_request()]}],
        },
    })["data"]
    return data["id"], data["url"], policy


def get_upload(upload_id: str) -> dict:
    return _request("GET", f"/video/v1/uploads/{upload_id}")["data"]


def get_asset(asset_id: str) -> dict:
    return _request("GET", f"/video/v1/assets/{asset_id}")["data"]


def delete_asset(asset_id: str) -> None:
    _request("DELETE", f"/video/v1/assets/{asset_id}")


def cancel_upload(upload_id: str) -> None:
    try:
        _request("PUT", f"/video/v1/uploads/{upload_id}/cancel")
    except MuxError:
        pass  # ya terminó o expiró: no hay nada que cancelar


def asset_fields(asset: dict) -> dict:
    """Campos de Training a partir de un asset de Mux."""
    from app.models import TRAINING_ERRORED, TRAINING_PROCESSING, TRAINING_READY

    status = {"ready": TRAINING_READY, "errored": TRAINING_ERRORED}.get(asset.get("status"), TRAINING_PROCESSING)
    playback = next(iter(asset.get("playback_ids") or []), None)
    fields = {"mux_asset_id": asset["id"], "video_status": status}
    if playback:
        fields["mux_playback_id"] = playback["id"]
        fields["playback_policy"] = playback.get("policy", "signed")
    if asset.get("duration"):
        fields["duration"] = int(round(asset["duration"]))
    return fields


def refresh(training) -> bool:
    """Consulta a Mux el estado de un video que aún no está listo. True si cambió."""
    from app.models import TRAINING_ERRORED, TRAINING_READY

    if training.video_status in (TRAINING_READY, TRAINING_ERRORED) or not enabled():
        return False
    before = (training.video_status, training.mux_asset_id, training.mux_playback_id)
    if not training.mux_asset_id and training.mux_upload_id:
        upload = get_upload(training.mux_upload_id)
        if upload.get("status") in ("errored", "cancelled", "timed_out"):
            training.video_status = TRAINING_ERRORED
        training.mux_asset_id = upload.get("asset_id")
    if training.mux_asset_id:
        for key, value in asset_fields(get_asset(training.mux_asset_id)).items():
            setattr(training, key, value)
    return before != (training.video_status, training.mux_asset_id, training.mux_playback_id)


# ── Transcripción y capítulos ────────────────────────────────────────────────
# Subtítulos: Mux los genera con Whisper (`generated_subtitles`, sin costo
# extra). Capítulos: trabajo `generate-chapters` de Mux Robots, que lee esos
# subtítulos. `sync_extras` avanza lo que falte; lo llaman el webhook
# (video.asset.track.*, robots.v1.job.generate_chapters.*) y la vista.

PENDING, READY, ERRORED = "pending", "ready", "errored"


def _captions_request() -> dict:
    lang = settings.mux_captions_language or "es"
    return {"language_code": lang, "name": "Español (automático)" if lang == "es" else f"{lang} (automático)"}


def _generated_track(asset: dict) -> dict | None:
    return next((t for t in asset.get("tracks") or []
                 if t.get("type") == "text" and t.get("text_source") == "generated_vod"), None)


def _audio_track(asset: dict) -> dict | None:
    return next((t for t in asset.get("tracks") or [] if t.get("type") == "audio"), None)


def _transcript_text(training, track_id: str) -> str:
    url = f"https://stream.mux.com/{training.mux_playback_id}/text/{track_id}.txt"
    params = {"token": _token(training.mux_playback_id, "v")} if training.playback_policy == "signed" else None
    try:
        r = httpx.get(url, params=params, timeout=30)
    except httpx.HTTPError as e:
        raise MuxError(f"No se pudo leer la transcripción: {e}") from e
    if r.status_code >= 400:
        raise MuxError(f"Transcripción: Mux respondió {r.status_code}")
    return r.text.strip()


def _job(resp: dict) -> dict:
    return resp.get("data", resp)  # el job puede venir envuelto en "data", como el resto de la API


def extras_done(training) -> bool:
    return training.captions_status == ERRORED or training.chapters_status in (READY, ERRORED)


def sync_extras(training) -> bool:
    """Avanza subtítulos → transcripción → capítulos de un video listo. True si cambió algo."""
    from app.models import TRAINING_READY

    if training.video_status != TRAINING_READY or not training.mux_asset_id or not enabled() or extras_done(training):
        return False
    before = (training.captions_status, training.chapters_status, training.chapters_job_id)

    if training.captions_status != READY:
        asset = get_asset(training.mux_asset_id)
        track = _generated_track(asset)
        if not track:
            audio = _audio_track(asset)
            if not audio:
                training.captions_status = ERRORED  # video sin audio: no hay qué transcribir
            elif training.captions_status is None:
                # Videos subidos antes de pedir subtítulos al crear: se piden ahora.
                _request("POST", f"/video/v1/assets/{training.mux_asset_id}/tracks/{audio['id']}/generate-subtitles",
                         json={"generated_subtitles": [_captions_request()]})
                training.captions_status = PENDING
        elif track.get("status") == "ready":
            training.captions_track_id = track["id"]
            training.transcript = _transcript_text(training, track["id"])
            training.captions_status = READY
        elif track.get("status") == "errored":
            training.captions_status = ERRORED
        else:
            training.captions_status = PENDING

    if training.captions_status == READY:
        if not training.chapters_job_id:
            params = {"asset_id": training.mux_asset_id}
            if settings.mux_captions_language not in ("", "auto"):
                params["language_code"] = settings.mux_captions_language
            job = _job(_request("POST", "/robots/v1/jobs/generate-chapters", json={"parameters": params}))
            training.chapters_job_id, training.chapters_status = job["id"], PENDING
        else:
            job = _job(_request("GET", f"/robots/v1/jobs/generate-chapters/{training.chapters_job_id}"))
            if job.get("status") == "completed":
                chapters = (job.get("outputs") or {}).get("chapters") or []
                training.chapters = [
                    {"start_time": float(c.get("start_time") or 0), "title": str(c.get("title") or "")[:200]}
                    for c in chapters if c.get("title")
                ]
                training.chapters_status = READY
            elif job.get("status") in ("errored", "cancelled"):
                training.chapters_status = ERRORED
    return before != (training.captions_status, training.chapters_status, training.chapters_job_id)


def verify_webhook(body: bytes, header: str) -> bool:
    """Valida el header Mux-Signature (`t=<ts>,v1=<hmac sha256 de "t.body">`)."""
    if not settings.mux_webhook_secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t", ""), parts.get("v1", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > WEBHOOK_TOLERANCE:
        return False
    expected = hmac.new(settings.mux_webhook_secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _private_key() -> str:
    raw = settings.mux_signing_key_private.strip()
    return raw if raw.startswith("-----BEGIN") else base64.b64decode(raw).decode()


def _token(playback_id: str, aud: str) -> str:
    """JWT RS256 de Mux: aud v (video), t (miniatura), s (storyboard)."""
    return jwt.encode(
        {"sub": playback_id, "aud": aud, "exp": int(time.time()) + TOKEN_TTL, "kid": settings.mux_signing_key_id},
        _private_key(), algorithm="RS256", headers={"kid": settings.mux_signing_key_id},
    )


def playback_tokens(playback_id: str) -> dict:
    """Tokens firmados para mux-player: video, miniatura y storyboard."""
    return {"playback": _token(playback_id, "v"), "thumbnail": _token(playback_id, "t"),
            "storyboard": _token(playback_id, "s")}


def thumbnail_url(training) -> str | None:
    if not training.mux_playback_id:
        return None
    url = f"https://image.mux.com/{training.mux_playback_id}/thumbnail.webp"
    if training.playback_policy == "signed":
        return f"{url}?token={_token(training.mux_playback_id, 't')}"
    return f"{url}?width=640"
