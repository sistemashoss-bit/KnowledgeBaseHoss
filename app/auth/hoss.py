"""Cliente de SSO contra hoss-api.

hoss-api es el proveedor de identidad del staff. knowledge NO comparte el
JWT_SECRET de hoss: valida credenciales llamando a un solo endpoint que
devuelve la identidad verificada.

    POST /users/verify-auth {email, password}
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
