from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_auth
from app.auth.provisioning import provision_from_identity
from app.auth.utils import (
    AUTH_COOKIE,
    create_access_token,
    generate_csrf_token,
    set_auth_cookie,
)
from app.database import get_db
from app import valkey_client as vk
from app.auth import hoss
from app.models import Department
from app.templating import templates

router = APIRouter(prefix="/auth", tags=["auth"])


# ── Login ─────────────────────────────────────────────────────────────────────
# Sin formulario propio: se entra desde la extensión (o el menú de hoss-front) por
# /auth/sso. Sin sesión solo se muestra el aviso.

def _access_page(request: Request, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        request, "access.html", {"error": error}, status_code=status_code
    )


@router.get("/login")
def login_page(request: Request, user=Depends(get_current_user)):
    if user:
        return RedirectResponse("/documents/", status_code=302)
    return _access_page(request)


# ── Logout ────────────────────────────────────────────────────────────────────
def _safe_logout_next(next_url: str | None) -> str | None:
    """Destino tras cerrar sesión: solo https dentro de hoss.com.mx (la cadena de
    cierre de sesión de HossExtension pasa por cada sitio y termina en hoss-front)."""
    if not next_url:
        return None
    parts = urlsplit(next_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not (host == "hoss.com.mx" or host.endswith(".hoss.com.mx")):
        return None
    return next_url


@router.get("/logout")
def logout(next: str | None = None):
    """Borra la cookie de sesión. Es GET y sin CSRF a propósito: lo abre
    HossExtension (o la app de escritorio) como navegación encadenada por todos
    los sitios, y lo peor que puede hacer un tercero es cerrarte la sesión."""
    resp = RedirectResponse(_safe_logout_next(next) or "/auth/login", status_code=302)
    resp.delete_cookie(AUTH_COOKIE, httponly=True, samesite="lax")
    return resp


# ── SSO handoff desde hoss-front ────────────────────────────────────────────────

def _safe_next(next_url: str | None) -> str | None:
    """Solo rutas relativas de este sitio: evita usar el handoff como open redirect."""
    if not next_url or not next_url.startswith("/") or next_url.startswith(("//", "/\\")):
        return None
    return next_url


@router.post("/sso")
async def sso_handoff(
    request: Request,
    token: str = Form(...),
    next: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Recibe el accessToken de hoss-api (el que NextAuth ya guarda en la sesión
    de hoss-front), lo valida contra hoss-api y abre sesión local. Sin segundo
    login. El token viaja por POST para no filtrarlo en URL/referer/logs.
    `next`: ruta a la que ir al entrar (p. ej. la de una notificación de la
    extensión); sin ella, a /documents/."""
    try:
        identity = await hoss.introspect_session(token)
    except hoss.AccessDenied:
        return _access_page(request, "No tienes acceso a Knowledge Base.", 403)
    if not identity:
        return _access_page(request, "Tu sesión de Hoss expiró.", 401)

    user = provision_from_identity(identity, db)
    if not user or not user.is_active:
        return _access_page(request, "Tu usuario está desactivado.", 403)

    # Guardamos el accessToken de hoss para reusarlo en llamadas server-to-server
    # (p. ej. sincronizar sucursales/regiones). hoss valida su expiración al usarlo.
    vk.store_hoss_token(user.id, token)

    access = create_access_token(str(user.id), user.role, user.department_id)
    resp = RedirectResponse(_safe_next(next) or "/documents/", status_code=302)
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
# firma la URL al renderizar. La subida del avatar vive en hoss-api
# (POST /users/me/avatar).

