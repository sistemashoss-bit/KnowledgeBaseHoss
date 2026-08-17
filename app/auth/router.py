import io
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_auth
from app.auth.utils import (
    create_access_token,
    generate_csrf_token,
    verify_csrf_token,
)
from app.config import settings
from app.database import get_db
from app import storage, valkey_client as vk
from app.auth import hoss
from app.models import ROLE_EMPLOYEE, Department, User
from app.templating import templates

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Login ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if user:
        return RedirectResponse("/documents/", status_code=302)
    has_users = db.query(User).count() > 0
    return templates.TemplateResponse(request, "login.html", {"has_users": has_users})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    if vk.is_rate_limited(email):
        has_users = db.query(User).count() > 0
        return templates.TemplateResponse(
            request, "login.html",
            {"has_users": has_users, "error": "Demasiados intentos fallidos. Intenta en 5 minutos."},
            status_code=429,
        )

    user = await _resolve_user(email, password, db)

    if not user or not user.is_active:
        vk.record_login_failure(email)
        has_users = db.query(User).count() > 0
        return templates.TemplateResponse(
            request, "login.html",
            {"has_users": has_users, "error": "Credenciales incorrectas"},
            status_code=400,
        )

    vk.clear_login_failures(email)

    token = create_access_token(str(user.id), user.role, user.department_id)
    resp = RedirectResponse("/documents/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=_max_age())
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie("access_token")
    return resp


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    dept = (
        db.query(Department).filter(Department.id == user.department_id).first()
        if user.department_id
        else None
    )
    csrf = generate_csrf_token(str(user.id))
    return templates.TemplateResponse(
        request, "profile.html",
        {"current_user": user, "dept": dept, "csrf_token": csrf},
    )


@router.post("/profile/name")
def update_name(
    name: str = Form(default=""),
    csrf_token: str = Form(...),
    user=Depends(require_auth),
    db: Session = Depends(get_db),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    user.name = name.strip() or None
    db.commit()
    return RedirectResponse("/auth/profile", status_code=302)


@router.post("/profile/avatar")
async def upload_avatar_endpoint(
    avatar: UploadFile = File(...),
    csrf_token: str = Form(...),
    user=Depends(require_auth),
    db: Session = Depends(get_db),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    content = await avatar.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "Imagen demasiado grande (máx 5 MB)")
    if not avatar.content_type or not avatar.content_type.startswith("image/"):
        raise HTTPException(400, "Solo se permiten imágenes")

    try:
        from PIL import Image
        img = Image.open(io.BytesIO(content)).convert("RGB")
        img.thumbnail((256, 256), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        img_bytes = buf.getvalue()
    except Exception:
        raise HTTPException(400, "No se pudo procesar la imagen")

    if user.avatar_key:
        try:
            storage.delete_avatar(user.avatar_key)
        except Exception:
            pass

    key = f"avatars/{user.id}.jpg"
    storage.upload_avatar(key, img_bytes)
    user.avatar_key = key
    db.commit()
    return RedirectResponse("/auth/profile", status_code=302)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _resolve_user(email: str, password: str, db: Session):
    """Full SSO: autentica contra hoss y aprovisiona/enlaza el usuario local."""
    identity = await hoss.verify_auth(email, password)
    if not identity:
        return None
    return _provision_from_identity(identity, db)


def _provision_from_identity(identity: dict, db: Session):
    """JIT provisioning: encuentra al usuario por corporate_id, o lo enlaza por
    email (pre-aprovisionamiento/backfill), o lo crea con rol genérico."""
    corporate_id = identity["corporate_id"]
    name = " ".join(
        p for p in (identity.get("first_name"), identity.get("last_name")) if p
    ) or None

    user = db.query(User).filter(User.corporate_id == corporate_id).first()
    if user:
        if name and user.name != name:
            user.name = name
            db.commit()
        return user

    # Enlaza un usuario pre-aprovisionado por el admin (o preexistente) por email,
    # conservando el rol/depto que ya tenga asignado.
    user = db.query(User).filter(User.email == identity["email"]).first()
    if user:
        user.corporate_id = corporate_id
        if name and not user.name:
            user.name = name
        db.commit()
        return user

    # Usuario nuevo sin pre-aprovisionar: rol genérico.
    user = User(
        id=uuid.uuid4(),
        corporate_id=corporate_id,
        email=identity["email"],
        name=name,
        role=ROLE_EMPLOYEE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _max_age() -> int:
    return settings.jwt_expire_minutes * 60
