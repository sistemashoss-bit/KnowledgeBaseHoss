import json
import os
import re
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

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


def _mentionify(html, names=None, me: str = "") -> Markup:
    # Después de linkify: resalta @Nombre/@todos de los participantes del grupo.
    from app.mentions import highlight
    return highlight(html, names or [], me)


templates.env.filters["mentionify"] = _mentionify


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


def _group_avatar_url(key: str | None) -> str | None:
    # Foto de grupo: sube/gestiona knowledge (bucket de chats), a diferencia del
    # avatar de usuario (dueño hoss-api, bucket aparte). Ver avatar_url arriba.
    if not key:
        return None
    return _storage.get_chat_file_url(key, "avatar", inline=True)


templates.env.globals["group_avatar_url"] = _group_avatar_url
# Markup(...) es obligatorio: sin marcarlo como seguro, el autoescape de Jinja
# convierte las comillas del JSON en &#34;, dejando el <script> con JS inválido
# (rompe silenciosamente cualquier `const x = {{ ... | tojson }}` — los gráficos
# de /reports/, el estado de mensajería y de tareas recurrentes que lo usan).
# Para que sea seguro, <, >, & y ' van como \u00XX (como el tojson de Jinja):
# un texto con "</script>" (p. ej. un nombre de usuario) no cierra el script, y
# JS/JSON los leen igual.
_JSON_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "'": "\\u0027"}


def _tojson(v) -> Markup:
    out = json.dumps(v, default=str)
    for ch, esc in _JSON_ESCAPES.items():
        out = out.replace(ch, esc)
    return Markup(out)


templates.env.filters["tojson"] = _tojson

from app.audit import action_label as _action_label
from app.auth.utils import generate_csrf_token as _generate_csrf_token

templates.env.filters["action_label"] = _action_label
templates.env.globals["csrf_for"] = lambda user_id: _generate_csrf_token(str(user_id)) if user_id else ""

from app.models import REACTION_EMOJIS as _REACTION_EMOJIS

templates.env.globals["reaction_emojis"] = _REACTION_EMOJIS


_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def _asset_version(name: str) -> str:
    # Rompe la caché del navegador cuando cambia un asset generado (tailwind.css
    # se reconstruye en cada build): un stat por render, sin leer el archivo.
    try:
        return str(int(os.stat(_ASSETS_DIR / name).st_mtime))
    except OSError:
        return "0"


templates.env.globals["asset_version"] = _asset_version


def _asset(name: str) -> str:
    """URL de un archivo de app/assets con ?v=<mtime>. /assets lo sirve con caché
    de un año cuando trae `v` (app.static_assets): cambiar el archivo cambia la URL."""
    return f"/assets/{name}?v={_asset_version(name)}"


templates.env.globals["asset"] = _asset
