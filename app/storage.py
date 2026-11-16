import math
import threading
import time
from functools import lru_cache

import boto3
from botocore.config import Config
from fastapi import HTTPException, UploadFile
from app.config import settings

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB por archivo


def check_upload_sizes(*files: UploadFile | None) -> None:
    """Rechaza (413) si algún archivo supera MAX_UPLOAD_BYTES. Llamar antes de
    cualquier efecto (DB, Wasabi) para no dejar subidas a medias."""
    for f in files:
        if not f or not f.filename:
            continue
        size = f.size
        if size is None:
            f.file.seek(0, 2)
            size = f.file.tell()
            f.file.seek(0)
        if size > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f'El archivo "{f.filename}" pesa {math.ceil(size / 104857.6) / 10:.1f} MB; '
                f"el límite es {MAX_UPLOAD_BYTES // 1048576} MB por archivo.",
            )


@lru_cache(maxsize=1)
def _client():
    # Uno por proceso (los clientes de boto3 son thread-safe): crearlo cuesta
    # ~3 ms, y una página firma decenas de avatares.
    return boto3.client(
        "s3",
        endpoint_url=settings.wasabi_endpoint_url,
        aws_access_key_id=settings.wasabi_access_key,
        aws_secret_access_key=settings.wasabi_secret_key,
        region_name=settings.wasabi_region,
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 2},
        ),
    )


def upload_file(file_key: str, content: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=settings.wasabi_bucket_name,
        Key=file_key,
        Body=content,
        ContentType=content_type,
    )


def get_signed_url(file_key: str, expiry: int = 900, filename: str | None = None) -> str:
    params: dict = {"Bucket": settings.wasabi_bucket_name, "Key": file_key}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    return _client().generate_presigned_url("get_object", Params=params, ExpiresIn=expiry)


def delete_file(file_key: str) -> None:
    _client().delete_object(Bucket=settings.wasabi_bucket_name, Key=file_key)


# Caché de URLs firmadas que se muestran una y otra vez (imágenes del
# chat). Cada firma es una URL distinta (lleva fecha y firma), así que firmar en
# cada render obliga al navegador a bajar la misma imagen en cada página. Se firma
# por 1 h y se reutiliza 50 min: la URL que se entrega siempre vale ≥ 10 min más.
_STABLE_TTL = 3600
_STABLE_REUSE = 3000
_stable_urls: dict[tuple, tuple[str, float]] = {}
_stable_lock = threading.Lock()


def _stable_url(params: dict) -> str:
    cache_key = tuple(sorted(params.items()))
    now = time.monotonic()
    hit = _stable_urls.get(cache_key)
    if hit and now - hit[1] < _STABLE_REUSE:
        return hit[0]
    url = _client().generate_presigned_url("get_object", Params=params, ExpiresIn=_STABLE_TTL)
    with _stable_lock:
        if len(_stable_urls) > 5000:  # tope de memoria: se vacía y se vuelve a llenar
            _stable_urls.clear()
        _stable_urls[cache_key] = (url, now)
    return url


# ── Avatares del staff (Bunny, sistema/avatars) ───────────────────────────────
# Viven en Bunny Storage y se leen del CDN en <cdn>/avatars/<corporate_id>: pública,
# sin firmar y siempre la última foto (el CDN no cachea). La suben hoss-front y
# demás apps al Edge Script de avatars; knowledge solo arma la URL. Como no se sabe
# si la persona tiene foto, quien la muestra cae a iniciales si el CDN responde 404
# (img[data-avatar] en base.html). El host va por entorno de hoss-api, sin env vars.
AVATAR_CDN_BY_API = {
    "api.hoss.com.mx": "https://hoss-avatars-prod-7gdw.b-cdn.net",
    "api-dev.hoss.com.mx": "https://hoss-avatars-dev-0962.b-cdn.net",
}

AVATAR_ASSET_PREFIX = "asset:"


def _avatar_cdn() -> str | None:
    from urllib.parse import urlparse
    return AVATAR_CDN_BY_API.get(urlparse(settings.hoss_api_url).netloc)


def get_avatar_url(user) -> str | None:
    """URL del avatar de un usuario, o None (se muestran iniciales).

    Una llave `asset:<archivo>` apunta a app/assets: la usa el usuario bot (MAX,
    con la mascota del widget), que no es staff de hoss."""
    if user is None:
        return None
    key = user.avatar_key or ""
    if key.startswith(AVATAR_ASSET_PREFIX):
        return f"/assets/{key.removeprefix(AVATAR_ASSET_PREFIX)}"
    cdn = _avatar_cdn()
    if not cdn or not user.corporate_id:
        return None
    return f"{cdn}/avatars/{str(user.corporate_id).lower()}"


# ── Evidence bucket ───────────────────────────────────────────────────────────

def upload_evidence(key: str, content: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=settings.wasabi_evidence_bucket_name,
        Key=key,
        Body=content,
        ContentType=content_type,
    )


def get_evidence_url(key: str, filename: str, expiry: int = 900, inline: bool = False) -> str:
    disposition = "inline" if inline else "attachment"
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.wasabi_evidence_bucket_name,
            "Key": key,
            "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
        },
        ExpiresIn=expiry,
    )


def delete_evidence(key: str) -> None:
    _client().delete_object(Bucket=settings.wasabi_evidence_bucket_name, Key=key)


# ── Chat attachments bucket ───────────────────────────────────────────────────

def upload_chat_file(key: str, content: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=settings.wasabi_chats_bucket_name,
        Key=key,
        Body=content,
        ContentType=content_type,
    )


def read_chat_file(key: str) -> bytes:
    return _client().get_object(Bucket=settings.wasabi_chats_bucket_name, Key=key)["Body"].read()


def get_chat_file_url(key: str, filename: str, inline: bool = False) -> str:
    """Adjuntos e imágenes del chat (y fotos de grupo): URL estable, ver _stable_url."""
    disposition = "inline" if inline else "attachment"
    return _stable_url({
        "Bucket": settings.wasabi_chats_bucket_name,
        "Key": key,
        "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
    })


def copy_chat_file(src_key: str, dst_key: str) -> None:
    """Copia dentro del bucket de chats sin descargar (mensajes reenviados)."""
    bucket = settings.wasabi_chats_bucket_name
    _client().copy_object(Bucket=bucket, Key=dst_key, CopySource={"Bucket": bucket, "Key": src_key})


def copy_chat_file_to_evidence(src_key: str, dst_key: str) -> None:
    """Copia un adjunto del chat al bucket de evidencias sin descargarlo (MAX lo
    agrega a una tarea o ticket). Es una copia, no una referencia: los adjuntos
    del chat se borran al vencer su retención y la evidencia debe quedarse."""
    _client().copy_object(
        Bucket=settings.wasabi_evidence_bucket_name, Key=dst_key,
        CopySource={"Bucket": settings.wasabi_chats_bucket_name, "Key": src_key},
    )


def delete_chat_file(key: str) -> None:
    _client().delete_object(Bucket=settings.wasabi_chats_bucket_name, Key=key)


# ── Whiteboards bucket ────────────────────────────────────────────────────────
# Imágenes pegadas en las pizarras: `{board_id}/{file_id}`. No se entregan con
# URL firmada sino a través de la app (whiteboards/router.py): el editor las
# necesita como dataURL y leerlas con fetch desde Wasabi pediría CORS en el bucket.

def upload_whiteboard_file(key: str, content: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=settings.wasabi_whiteboards_bucket_name,
        Key=key,
        Body=content,
        ContentType=content_type,
    )


def read_whiteboard_file(key: str) -> bytes:
    obj = _client().get_object(Bucket=settings.wasabi_whiteboards_bucket_name, Key=key)
    return obj["Body"].read()


def delete_whiteboard_files(keys: list[str]) -> None:
    bucket = settings.wasabi_whiteboards_bucket_name
    for i in range(0, len(keys), 1000):  # tope de delete_objects por llamada
        _client().delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True},
        )
