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


# ── Avatar bucket (separate) ──────────────────────────────────────────────────
# hoss-api es dueño del avatar (SSO) y de la subida (POST /users/me/avatar). knowledge
# solo recibe la llave en el payload de identidad y firma la URL de lectura localmente
# (comparte el mismo Wasabi/bucket). Por eso aquí solo queda el presign de lectura.

# Caché de URLs firmadas que se muestran una y otra vez (avatares, imágenes del
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


def get_avatar_signed_url(key: str) -> str:
    """URL firmada (local, sin red) y estable por 50 min; ver _stable_url."""
    return _stable_url({"Bucket": settings.wasabi_avatar_bucket_name, "Key": key})


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


def get_chat_file_url(key: str, filename: str, inline: bool = False) -> str:
    """Adjuntos e imágenes del chat (y fotos de grupo): URL estable, ver _stable_url."""
    disposition = "inline" if inline else "attachment"
    return _stable_url({
        "Bucket": settings.wasabi_chats_bucket_name,
        "Key": key,
        "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
    })


def delete_chat_file(key: str) -> None:
    _client().delete_object(Bucket=settings.wasabi_chats_bucket_name, Key=key)
