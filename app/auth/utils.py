from datetime import datetime, timedelta

from fastapi import Response
from itsdangerous import BadSignature, URLSafeSerializer
from jose import jwt

from app.config import settings


AUTH_COOKIE = "access_token"


def session_expiry(max_until: datetime | None = None) -> datetime:
    """Fin de la sesión (UTC naive): la duración normal o, si las políticas de
    identidad la topan (cierre del horario, `session_max_until`), lo que llegue antes."""
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return min(expire, max_until) if max_until else expire


def set_auth_cookie(response: Response, token: str, expires_at: datetime | None = None) -> None:
    """Emite la cookie de sesión de knowledge de forma consistente."""
    max_age = settings.jwt_expire_minutes * 60
    if expires_at:
        max_age = max(0, int((expires_at - datetime.utcnow()).total_seconds()))
    response.set_cookie(
        AUTH_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=max_age,
    )


def create_access_token(user_id: str, role: str, department_id, expires_at: datetime | None = None) -> str:
    expire = expires_at or session_expiry()
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
