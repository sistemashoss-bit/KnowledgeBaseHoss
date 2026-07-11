from fastapi import Depends, HTTPException, Request
from jose import JWTError
from sqlalchemy.orm import Session

from app.auth.utils import decode_token
from app.database import get_db
from app.models import User
from app import valkey_client as vk


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get("access_token")
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
