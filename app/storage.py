import boto3
from botocore.config import Config
from app.config import settings


def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.wasabi_endpoint_url,
        aws_access_key_id=settings.wasabi_access_key,
        aws_secret_access_key=settings.wasabi_secret_key,
        region_name=settings.wasabi_region,
        config=Config(signature_version="s3v4"),
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

def upload_avatar(key: str, content: bytes) -> None:
    _client().put_object(
        Bucket=settings.wasabi_avatar_bucket_name,
        Key=key,
        Body=content,
        ContentType="image/jpeg",
    )


def delete_avatar(key: str) -> None:
    _client().delete_object(Bucket=settings.wasabi_avatar_bucket_name, Key=key)


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


def get_evidence_url(key: str, filename: str, expiry: int = 900) -> str:
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.wasabi_evidence_bucket_name,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{filename}"',
        },
        ExpiresIn=expiry,
    )


def delete_evidence(key: str) -> None:
    _client().delete_object(Bucket=settings.wasabi_evidence_bucket_name, Key=key)
