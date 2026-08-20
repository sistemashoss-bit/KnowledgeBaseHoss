from datetime import datetime, timedelta

from fastapi import Response
from itsdangerous import BadSignature, URLSafeSerializer
from jose import jwt

from app.config import settings


AUTH_COOKIE = "access_token"


def set_auth_cookie(response: Response, token: str) -> None:
    """Emite la cookie de sesión de knowledge de forma consistente."""
    response.set_cookie(
        AUTH_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_expire_minutes * 60,
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE)


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
