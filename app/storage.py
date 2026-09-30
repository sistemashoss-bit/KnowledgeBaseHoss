import math

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


def _client():
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

def get_avatar_signed_url(key: str, expiry: int = 3600) -> str:
    """Presigned URL valid for 1 hour — computed locally, no network call."""
    return _client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.wasabi_avatar_bucket_name, "Key": key},
        ExpiresIn=expiry,
    )


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


def get_chat_file_url(key: str, filename: str, expiry: int = 900, inline: bool = False) -> str:
    disposition = "inline" if inline else "attachment"
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.wasabi_chats_bucket_name,
            "Key": key,
            "ResponseContentDisposition": f'{disposition}; filename="{filename}"',
        },
        ExpiresIn=expiry,
    )


def delete_chat_file(key: str) -> None:
    _client().delete_object(Bucket=settings.wasabi_chats_bucket_name, Key=key)
