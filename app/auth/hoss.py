"""Cliente de SSO contra hoss-api.

hoss-api es el proveedor de identidad del staff. knowledge NO comparte el
JWT_SECRET de hoss: valida credenciales (login propio) o el token del handoff
(extensión / hoss-front) preguntándole a hoss-api por la identidad.

    POST /sso/verify-auth {email, password, site}
    GET  /sso/identity?site=<site>   (Authorization: Bearer <sesion_hoss>)
        -> {corporate_id, id, email, first_name, last_name, role, site_role, position, department, avatar_key, avatar_url}

    Responden 403 si el usuario no tiene el permiso del sitio en hoss
    (knowledge_access) o, en verify-auth, si lo niegan las políticas de identidad
    (dispositivo, horario); se levanta AccessDenied.

    avatar_key: llave del avatar en Wasabi (bucket compartido hossavatars); knowledge
    firma la URL localmente. avatar_url: URL ya firmada por hoss (para consumidores que
    no comparten el mismo Wasabi).
"""
import httpx

from app.config import settings

# Id de este sitio en el SSO de hoss-api (constants/ssoSites.js).
SITE = "knowledge"


class AccessDenied(Exception):
    """Identidad válida en hoss, pero sin acceso a este sitio (403). `message`: el
    motivo que manda hoss-api cuando lo niega una política (p. ej. "Este equipo no
    está autorizado…"); None si es solo falta de permiso."""

    def __init__(self, message: str | None = None):
        super().__init__(message)
        self.message = message


class RateLimited(Exception):
    """hoss-api bloqueó el email por intentos fallidos (429). Trae su mensaje."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


async def verify_auth(email: str, password: str) -> dict | None:
    """Login propio: devuelve la identidad si las credenciales son válidas en hoss;
    None si no. El rate-limit por email lo lleva hoss-api (RateLimited)."""
    base = settings.hoss_api_url.rstrip("/")
    if not base:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base}/sso/verify-auth",
                json={"email": email, "password": password, "site": SITE},
            )
    except httpx.HTTPError:
        return None

    if resp.status_code in (403, 429):
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code == 429:
            raise RateLimited(body.get("error") or "Demasiados intentos. Intenta más tarde.")
        # Con `reason` lo negó una política de identidad; sin él, falta el permiso.
        raise AccessDenied(body.get("error") if body.get("reason") else None)
    if resp.status_code != 200:
        return None
    identity = resp.json()
    if not identity.get("corporate_id"):
        return None
    return identity


async def introspect_session(session_token: str) -> dict | None:
    """Valida una sesión existente de hoss (cookie de .hoss.com.mx) y devuelve
    la identidad. Base del SSO silencioso:
    knowledge NO valida el token localmente, se lo pregunta a hoss-api.
    Levanta AccessDenied si el usuario no tiene acceso al sitio."""
    base = settings.hoss_api_url.rstrip("/")
    if not base or not session_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base}/sso/identity",
                params={"site": SITE},
                headers={"Authorization": f"Bearer {session_token}"},
            )
            if resp.status_code == 403:
                raise AccessDenied()
            if resp.status_code != 200:
                return None
            identity = resp.json()
            if not identity.get("corporate_id"):
                return None
            return identity
    except httpx.HTTPError:
        return None


async def fetch_org(session_token: str) -> dict | None:
    """Trae regiones y sucursales de hoss usando el token del staff (SSO).

    hoss es el dueño del catálogo; cada región/sucursal trae su UUID global
    (global_region_id / global_branch_id) con el que knowledge las mapea.
    Devuelve {"regions": [...], "branches": [...]} o None si algo falla
    (token expirado/insuficiente, hoss no disponible, etc.).
    """
    base = settings.hoss_api_url.rstrip("/")
    if not base or not session_token:
        return None

    headers = {"Authorization": f"Bearer {session_token}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            regions_resp, branches_resp = None, None
            regions_resp = await client.get(f"{base}/regions/", headers=headers)
            branches_resp = await client.get(
                f"{base}/branches/all",
                headers=headers,
                params={"page": 1, "pageSize": 10000},
            )
            if regions_resp.status_code != 200 or branches_resp.status_code != 200:
                return None

            regions = regions_resp.json()
            branches_payload = branches_resp.json()
            # /branches/all responde { data, total, ... }; /regions/ es una lista.
            branches = (
                branches_payload.get("data", [])
                if isinstance(branches_payload, dict)
                else branches_payload
            )
            if not isinstance(regions, list) or not isinstance(branches, list):
                return None
            return {"regions": regions, "branches": branches}
    except httpx.HTTPError:
        return None


async def fetch_departments(session_token: str) -> list | None:
    """Trae los departamentos de hoss con el token del staff (requiere
    view_departments en hoss). Cada uno trae su global_department_id, con el que
    knowledge lo mapea. None si algo falla (token expirado/sin permiso, hoss caído)."""
    base = settings.hoss_api_url.rstrip("/")
    if not base or not session_token:
        return None

    headers = {"Authorization": f"Bearer {session_token}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{base}/departments", headers=headers)
            if resp.status_code != 200:
                return None
            departments = resp.json()
            return departments if isinstance(departments, list) else None
    except httpx.HTTPError:
        return None
