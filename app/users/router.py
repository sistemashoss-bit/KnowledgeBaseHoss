"""
Two routers in this file:
- api_router  → /api/users  (JSON, for Swagger)
- mgmt_router → /users      (HTML, for admin/superadmin UI)
"""
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.auth.utils import generate_csrf_token, hash_password, verify_csrf_token
from app.database import get_db
from app.models import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_SUPERADMIN, ROLES, Department, User
from app.permissions import can_manage_user
from app.templating import templates

# ── JSON API (Swagger) ────────────────────────────────────────────────────────

api_router = APIRouter(prefix="/api/users", tags=["users-api"])


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: str = "employee"
    department_id: str | None = None


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    password: str | None = None
    role: str | None = None
    department_id: str | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    id: str
    email: str
    role: str
    department_id: str | None
    is_active: bool

    model_config = {"from_attributes": True}


@api_router.get("/", response_model=list[UserOut])
def list_users_api(db: Session = Depends(get_db), _=Depends(require_role(ROLE_SUPERADMIN))):
    return db.query(User).order_by(User.email).all()


@api_router.post("/", response_model=UserOut, status_code=201)
def create_user_api(
    data: UserCreate,
    db: Session = Depends(get_db),
    _=Depends(require_role(ROLE_SUPERADMIN)),
):
    if data.role not in ROLES:
        raise HTTPException(400, f"role must be one of {ROLES}")
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(400, "Email already registered")
    u = User(
        id=uuid.uuid4(),
        email=data.email,
        password_hash=hash_password(data.password),
        role=data.role,
        department_id=data.department_id,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@api_router.get("/{user_id}", response_model=UserOut)
def get_user_api(user_id: str, db: Session = Depends(get_db), _=Depends(require_role(ROLE_SUPERADMIN))):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404)
    return u


@api_router.patch("/{user_id}", response_model=UserOut)
def update_user_api(
    user_id: str,
    data: UserUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_role(ROLE_SUPERADMIN)),
):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404)
    if data.email is not None:
        u.email = data.email
    if data.password is not None:
        u.password_hash = hash_password(data.password)
    if data.role is not None:
        if data.role not in ROLES:
            raise HTTPException(400, f"role must be one of {ROLES}")
        u.role = data.role
    if data.department_id is not None:
        u.department_id = data.department_id
    if data.is_active is not None:
        u.is_active = data.is_active
    db.commit()
    db.refresh(u)
    return u


@api_router.delete("/{user_id}", status_code=204)
def delete_user_api(user_id: str, db: Session = Depends(get_db), _=Depends(require_role(ROLE_SUPERADMIN))):
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(404)
    db.delete(u)
    db.commit()


# ── HTML Management ───────────────────────────────────────────────────────────

mgmt_router = APIRouter(prefix="/users", tags=["user-management"])


@mgmt_router.get("/", response_class=HTMLResponse)
def user_management(
    request: Request,
    db: Session = Depends(get_db),
    actor=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    if actor.role == ROLE_SUPERADMIN:
        users = db.query(User).order_by(User.role, User.email).all()
    else:  # admin: only employees of their department
        users = (
            db.query(User)
            .filter(User.department_id == actor.department_id, User.role == ROLE_EMPLOYEE)
            .order_by(User.email)
            .all()
        )

    departments = db.query(Department).order_by(Department.name).all()
    csrf = generate_csrf_token(str(actor.id))
    return templates.TemplateResponse(
        request, "users/list.html",
        {
            "users": users,
            "departments": departments,
            "current_user": actor,
            "csrf_token": csrf,
            "roles": ROLES,
        },
    )


@mgmt_router.post("/create")
def create_user_html(
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    department_id: str = Form(default=""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    actor=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    if not verify_csrf_token(csrf_token, str(actor.id)):
        raise HTTPException(403, "Invalid CSRF token")

    if actor.role == ROLE_ADMIN:
        role = ROLE_EMPLOYEE
        department_id = str(actor.department_id)
    elif role not in ROLES:
        raise HTTPException(400, f"Invalid role")

    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "Email already registered")

    db.add(User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(password),
        role=role,
        department_id=department_id or None,
    ))
    db.commit()
    return RedirectResponse("/users/", status_code=302)


@mgmt_router.post("/{user_id}/toggle")
def toggle_user(
    user_id: str,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    actor=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    if not verify_csrf_token(csrf_token, str(actor.id)):
        raise HTTPException(403, "Invalid CSRF token")
    target = db.query(User).filter(User.id == user_id).first()
    if not target or not can_manage_user(actor, target):
        raise HTTPException(403, "Access denied")
    target.is_active = not target.is_active
    db.commit()
    return RedirectResponse("/users/", status_code=302)


@mgmt_router.post("/{user_id}/reset-password")
def reset_password(
    user_id: str,
    new_password: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    actor=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    if not verify_csrf_token(csrf_token, str(actor.id)):
        raise HTTPException(403, "Invalid CSRF token")
    target = db.query(User).filter(User.id == user_id).first()
    if not target or not can_manage_user(actor, target):
        raise HTTPException(403, "Access denied")
    if len(new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    target.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse("/users/", status_code=302)
