import json
from fastapi.templating import Jinja2Templates
from app import storage as _storage

templates = Jinja2Templates(directory="app/templates")


def _avatar_url(key: str | None) -> str | None:
    # hoss-api (SSO) es dueño del avatar y manda la LLAVE en el payload de identidad.
    # Como knowledge comparte el mismo Wasabi/bucket (hossavatars), firma la URL él
    # mismo al renderizar (fresca, sin cachear URLs que expiran).
    if not key:
        return None
    return _storage.get_avatar_signed_url(key)


templates.env.globals["avatar_url"] = _avatar_url


def _evidence_url(key: str, filename: str) -> str:
    return _storage.get_evidence_url(key, filename)


templates.env.globals["evidence_url"] = _evidence_url
templates.env.filters["tojson"] = lambda v: json.dumps(v)
