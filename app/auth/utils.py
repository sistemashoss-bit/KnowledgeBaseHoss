from datetime import datetime, timedelta

from itsdangerous import BadSignature, URLSafeSerializer
from jose import jwt

from app.config import settings


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
