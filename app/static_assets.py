"""/assets con caché HTTP.

Las plantillas piden los archivos con asset() (app/templating.py), que agrega
?v=<mtime>: esa URL cambia en cuanto cambia el archivo, así que se puede cachear
un año sin volver a preguntar (`immutable`). Sin `v` (una URL escrita a mano) se
revalida en cada uso, como antes (ETag/Last-Modified → 304).
"""
from urllib.parse import parse_qs

from fastapi.staticfiles import StaticFiles

_IMMUTABLE = "public, max-age=31536000, immutable"


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            versioned = "v" in parse_qs(scope.get("query_string", b"").decode())
            response.headers["Cache-Control"] = _IMMUTABLE if versioned else "no-cache"
        return response
