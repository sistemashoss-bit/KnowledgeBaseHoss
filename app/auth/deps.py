from fastapi import Depends, HTTPException, Request
from jose import JWTError
from sqlalchemy.orm import Session

from app.auth.utils import AUTH_COOKIE, decode_token, verify_csrf_token
from app.database import get_db
from app.models import User
from app import valkey_client as vk

CSRF_HEADER = "X-CSRF-Token"
_CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Sin sesión todavía (login/SSO) o cerrarla: no hay token de usuario que validar.
_CSRF_EXEMPT_PATHS = {"/auth/login", "/auth/logout", "/auth/sso"}


async def require_csrf(request: Request) -> None:
    """Dependencia global (ver main.py): valida CSRF en todo request que modifica.

    El token viene del header X-CSRF-Token (htmx y fetch, puesto en base.html)
    o del campo `csrf_token` de formularios normales. Solo aplica con sesión
    válida: sin cookie no hay nada que un sitio ajeno pueda aprovechar, y el
    endpoint ya rechaza por su cuenta si requiere usuario. Decodifica el JWT sin
    tocar la DB para no abrir una sesión por request.
    """
    if request.method in _CSRF_SAFE_METHODS or request.url.path in _CSRF_EXEMPT_PATHS:
        return
    cookie = request.cookies.get(AUTH_COOKIE)
    if not cookie:
        return
    try:
        user_id = decode_token(cookie).get("sub")
    except JWTError:
        return
    if not user_id:
        return

    token = request.headers.get(CSRF_HEADER)
    if not token and request.headers.get("content-type", "").startswith(
        ("multipart/form-data", "application/x-www-form-urlencoded")
    ):
        # FastAPI ya parseó el form para el endpoint; Starlette lo tiene en caché.
        token = (await request.form()).get("csrf_token")
    if not token or not verify_csrf_token(str(token), str(user_id)):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        return None
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            return None
        user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
        if user:
            vk.update_last_seen(user.id)
        return user
    except JWTError:
        return None


def require_auth(user: User | None = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_role(*roles: str):
    def dep(user: User | None = Depends(get_current_user)) -> User:
        if not user:
            raise HTTPException(status_code=401, detail="Authentication required")
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return user

    return dep


def require_superadmin(user: User | None = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin required")
    return user
