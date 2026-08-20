"""Cliente de SSO contra hoss-api.

hoss-api es el proveedor de identidad del staff. knowledge NO comparte el
JWT_SECRET de hoss: valida credenciales llamando a un solo endpoint que
devuelve la identidad verificada.

    POST /users/verify-auth {email, password}
        -> {corporate_id, id, email, first_name, last_name, role}

    GET  /users/me   (Authorization: Bearer <sesion_hoss>)
        -> {corporate_id, id, email, first_name, last_name, role}
"""
import httpx

from app.config import settings


async def verify_auth(email: str, password: str) -> dict | None:
    """Devuelve la identidad si las credenciales son válidas en hoss; None si no."""
    base = settings.hoss_api_url.rstrip("/")
    if not base:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base}/users/verify-auth",
                json={"email": email, "password": password},
            )
            if resp.status_code != 200:
                return None
            identity = resp.json()
            if not identity.get("corporate_id"):
                return None
            return identity
    except httpx.HTTPError:
        return None


async def introspect_session(session_token: str) -> dict | None:
    """Valida una sesión existente de hoss (cookie de .hoss.com.mx) y devuelve
    la identidad, con el mismo shape que verify_auth. Base del SSO silencioso:
    knowledge NO valida el token localmente, se lo pregunta a hoss-api."""
    base = settings.hoss_api_url.rstrip("/")
    if not base or not session_token:
        return None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{base}/users/me",
                headers={"Authorization": f"Bearer {session_token}"},
            )
            if resp.status_code != 200:
                return None
            identity = resp.json()
            if not identity.get("corporate_id"):
                return None
            return identity
    except httpx.HTTPError:
        return None
