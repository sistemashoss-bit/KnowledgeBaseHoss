from fastapi.templating import Jinja2Templates
from app import storage as _storage

templates = Jinja2Templates(directory="app/templates")


def _avatar_url(key: str | None) -> str | None:
    if not key:
        return None
    return _storage.get_avatar_signed_url(key)


templates.env.globals["avatar_url"] = _avatar_url
