from fastapi import Depends, HTTPException, Request
from jose import JWTError
from sqlalchemy.orm import Session

from app.auth.utils import decode_token
from app.database import get_db
from app.models import User


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
        if not user_id:
            return None
        return db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
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
