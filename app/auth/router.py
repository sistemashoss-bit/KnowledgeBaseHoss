from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_auth
from app.auth.provisioning import provision_from_identity
from app.auth.utils import (
    clear_auth_cookie,
    create_access_token,
    generate_csrf_token,
    set_auth_cookie,
    verify_csrf_token,
)
from app.database import get_db
from app import valkey_client as vk
from app.auth import hoss
from app.models import Department, User
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
    set_auth_cookie(resp, token)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/", status_code=302)
    clear_auth_cookie(resp)
    return resp


# ── SSO handoff desde hoss-front ────────────────────────────────────────────────

@router.post("/sso")
async def sso_handoff(
    token: str = Form(...),
    db: Session = Depends(get_db),
):
    """Recibe el accessToken de hoss-api (el que NextAuth ya guarda en la sesión
    de hoss-front), lo valida contra hoss-api y abre sesión local. Sin segundo
    login. El token viaja por POST para no filtrarlo en URL/referer/logs."""
    identity = await hoss.introspect_session(token)
    if not identity:
        return RedirectResponse("/auth/login", status_code=302)

    user = provision_from_identity(identity, db)
    if not user or not user.is_active:
        return RedirectResponse("/auth/login", status_code=302)

    # Guardamos el accessToken de hoss para reusarlo en llamadas server-to-server
    # (p. ej. sincronizar sucursales/regiones). hoss valida su expiración al usarlo.
    vk.store_hoss_token(user.id, token)

    access = create_access_token(str(user.id), user.role, user.department_id)
    resp = RedirectResponse("/documents/", status_code=302)
    set_auth_cookie(resp, access)
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


# Nombre y avatar son identidad compartida: los administra hoss-api (SSO) y se
# sincronizan al iniciar sesión (ver provisioning.py). knowledge ya no edita el
# nombre ni sube avatares; recibe la llave del avatar en el payload de identidad y
# firma la URL
# al renderizar. La subida vive en hoss-api (POST /users/me/avatar).


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _resolve_user(email: str, password: str, db: Session):
    """Full SSO: autentica contra hoss y aprovisiona/enlaza el usuario local."""
    identity = await hoss.verify_auth(email, password)
    if not identity:
        return None
    return provision_from_identity(identity, db)
