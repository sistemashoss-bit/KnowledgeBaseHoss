import io
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_auth
from app.auth.utils import (
    create_access_token,
    create_pre_auth_token,
    generate_csrf_token,
    generate_totp_secret,
    hash_password,
    verify_csrf_token,
    verify_password,
    verify_pre_auth_token,
    verify_totp,
)
from app.config import settings
from app.database import get_db
from app import audit, storage, valkey_client as vk
from app.models import ROLE_SUPERADMIN, Department, User
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

    user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()

    if not user or not verify_password(password, user.password_hash):
        vk.record_login_failure(email)
        audit.log_action("login_fail", request=request, details=email)
        has_users = db.query(User).count() > 0
        return templates.TemplateResponse(
            request, "login.html",
            {"has_users": has_users, "error": "Credenciales incorrectas"},
            status_code=400,
        )

    vk.clear_login_failures(email)

    if user.totp_enabled:
        pre_auth = create_pre_auth_token(str(user.id))
        resp = RedirectResponse("/auth/totp", status_code=302)
        resp.set_cookie("pre_auth", pre_auth, httponly=True, samesite="lax", max_age=300)
        return resp

    token = create_access_token(str(user.id), user.role, user.department_id)
    audit.log_action("login", user=user, request=request)
    resp = RedirectResponse("/documents/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=_max_age())
    return resp


@router.get("/totp", response_class=HTMLResponse)
def totp_verify_page(request: Request):
    if not _valid_pre_auth(request):
        return RedirectResponse("/auth/login", status_code=302)
    return templates.TemplateResponse(request, "totp_verify.html", {})


@router.post("/totp")
def totp_verify(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    user_id = _valid_pre_auth(request)
    if not user_id:
        return templates.TemplateResponse(
            request, "totp_verify.html",
            {"error": "Sesión expirada, vuelve a iniciar sesión"},
            status_code=400,
        )

    user = db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()
    if not user or not verify_totp(user.totp_secret, code):
        return templates.TemplateResponse(
            request, "totp_verify.html",
            {"error": "Código incorrecto"},
            status_code=400,
        )

    token = create_access_token(str(user.id), user.role, user.department_id)
    audit.log_action("login_totp", user=user, request=request)
    resp = RedirectResponse("/documents/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=_max_age())
    resp.delete_cookie("pre_auth")
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/auth/login", status_code=302)
    resp.delete_cookie("access_token")
    resp.delete_cookie("pre_auth")
    return resp


# ── Register (first user only) ────────────────────────────────────────────────

@router.get("/register", response_class=HTMLResponse)
def register_page(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if db.query(User).count() > 0:
        return RedirectResponse("/auth/login", status_code=302)
    if user:
        return RedirectResponse("/documents/", status_code=302)
    return templates.TemplateResponse(request, "register.html", {})


@router.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if db.query(User).count() > 0:
        raise HTTPException(403, "Registration is closed")

    error = None
    if password != password_confirm:
        error = "Las contraseñas no coinciden"
    elif len(password) < 8:
        error = "La contraseña debe tener al menos 8 caracteres"
    elif db.query(User).filter(User.email == email).first():
        error = "Este correo ya está registrado"

    if error:
        return templates.TemplateResponse(
            request, "register.html", {"error": error}, status_code=400
        )

    new_user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(password),
        role=ROLE_SUPERADMIN,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    token = create_access_token(str(new_user.id), new_user.role, new_user.department_id)
    resp = RedirectResponse("/documents/", status_code=302)
    resp.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=_max_age())
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


# ── TOTP setup ────────────────────────────────────────────────────────────────

@router.get("/totp/setup", response_class=HTMLResponse)
def totp_setup_page(request: Request, user=Depends(require_auth)):
    import base64
    import pyotp, qrcode

    secret = generate_totp_secret()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="KnowledgeBase")
    buf = io.BytesIO()
    qrcode.make(uri).save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    csrf = generate_csrf_token(str(user.id))
    return templates.TemplateResponse(
        request, "setup_totp.html",
        {"secret": secret, "qr_b64": qr_b64, "csrf_token": csrf, "current_user": user},
    )


@router.post("/totp/setup")
def totp_setup(
    secret: str = Form(...),
    code: str = Form(...),
    csrf_token: str = Form(...),
    user=Depends(require_auth),
    db: Session = Depends(get_db),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    if not verify_totp(secret, code):
        raise HTTPException(400, "Código inválido")
    user.totp_secret = secret
    user.totp_enabled = True
    db.commit()
    return RedirectResponse("/auth/profile", status_code=302)


@router.post("/totp/disable")
def totp_disable(
    csrf_token: str = Form(...),
    user=Depends(require_auth),
    db: Session = Depends(get_db),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    user.totp_secret = None
    user.totp_enabled = False
    db.commit()
    return RedirectResponse("/auth/profile", status_code=302)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _max_age() -> int:
    return settings.jwt_expire_minutes * 60


def _valid_pre_auth(request: Request) -> str | None:
    token = request.cookies.get("pre_auth")
    return verify_pre_auth_token(token) if token else None
