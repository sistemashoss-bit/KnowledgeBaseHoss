from datetime import datetime, timedelta

import pyotp
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from itsdangerous import BadSignature, URLSafeSerializer, URLSafeTimedSerializer
from jose import jwt

from app.config import settings

_ph = PasswordHasher()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _ph.verify(hashed, plain)
    except VerifyMismatchError:
        return False


def create_access_token(user_id: str, role: str, department_id) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {
            "sub": str(user_id),
            "role": role,
            "dept": str(department_id) if department_id else None,
            "exp": expire,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def generate_csrf_token(user_id: str) -> str:
    return URLSafeSerializer(settings.csrf_secret).dumps(str(user_id))


def verify_csrf_token(token: str, user_id: str) -> bool:
    try:
        return URLSafeSerializer(settings.csrf_secret).loads(token) == str(user_id)
    except BadSignature:
        return False


def create_pre_auth_token(user_id: str) -> str:
    """Short-lived token (5 min) issued after password OK, before TOTP verified."""
    return URLSafeTimedSerializer(settings.jwt_secret).dumps(str(user_id), salt="pre-auth")


def verify_pre_auth_token(token: str) -> str | None:
    try:
        return URLSafeTimedSerializer(settings.jwt_secret).loads(
            token, salt="pre-auth", max_age=300
        )
    except Exception:
        return None


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def verify_totp(secret: str, code: str) -> bool:
    return pyotp.TOTP(secret).verify(code)
