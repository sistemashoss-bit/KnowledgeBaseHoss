import json
import re
from markupsafe import Markup, escape
from fastapi.templating import Jinja2Templates
from app import storage as _storage

templates = Jinja2Templates(directory="app/templates")

# http(s)://... o www.dominio.tld — sin espacios ni comillas/ángulos.
_URL_RE = re.compile(r'((?:https?://|www\.)[^\s<>"\']+)', re.IGNORECASE)
_TRAILING_PUNCT = '.,;:!?)]}"\''


def _linkify(text: str | None) -> Markup:
    """Escapa el texto y convierte URLs en <a> clickeables. Seguro ante XSS:
    todo pasa por escape() antes de insertarse; el regex corre sobre el texto
    ya escapado, así que nunca inyecta markup desde el contenido del usuario."""
    if not text:
        return Markup("")
    escaped = str(escape(text))

    def repl(m: re.Match) -> str:
        url = m.group(1)
        trailing = ""
        while url and url[-1] in _TRAILING_PUNCT:
            trailing = url[-1] + trailing
            url = url[:-1]
        href = url if url.lower().startswith("http") else f"https://{url}"
        return f'<a href="{href}" target="_blank" rel="noopener noreferrer" class="underline break-all">{url}</a>{trailing}'

    return Markup(_URL_RE.sub(repl, escaped))


templates.env.filters["linkify"] = _linkify


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
