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


# Mensaje de "compartir ubicación" del chat: su contenido es exactamente el enlace
# que arma app/messaging/helpers.py (osm_location_url). Se dibuja como tarjeta con mapa.
_OSM_LOCATION_RE = re.compile(
    r"https://www\.openstreetmap\.org/\?mlat=(-?\d{1,2}\.\d+)&mlon=(-?\d{1,3}\.\d+)#map=\d+/[-\d.]+/[-\d.]+"
)


def _osm_location(text: str | None) -> dict | None:
    m = _OSM_LOCATION_RE.fullmatch((text or "").strip())
    if not m:
        return None
    lat, lng = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    d = 0.003
    return {
        "lat": lat,
        "lng": lng,
        "url": m.group(0),
        "embed": (
            "https://www.openstreetmap.org/export/embed.html"
            f"?bbox={lng - d:.6f},{lat - d / 2:.6f},{lng + d:.6f},{lat + d / 2:.6f}"
            f"&layer=mapnik&marker={lat:.6f},{lng:.6f}"
        ),
    }


templates.env.filters["osm_location"] = _osm_location


def _sticker_url(text: str | None) -> str | None:
    # Mensaje-sticker del chat: su contenido es solo la URL del APNG (app/messaging/stickers.py).
    from app.messaging.stickers import STICKER_URL_RE
    text = (text or "").strip()
    return text if STICKER_URL_RE.fullmatch(text) else None


def _stickers_enabled() -> bool:
    from app.messaging.stickers import enabled
    return enabled()


templates.env.filters["sticker_url"] = _sticker_url


# Mensaje de "nueva pizarra" del chat (app/messaging/routes/sending.py
# create_chat_whiteboard): título + URL absoluta, para que se lea bien en
# notificaciones y reenvíos. En el chat se dibuja como tarjeta con enlace relativo.
_WHITEBOARD_MSG_RE = re.compile(
    r"🎨 Pizarra: (?P<title>[^\n]+)\n(?P<url>https?://\S+/whiteboards/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))"
)


def whiteboard_message(title: str, board_id) -> str:
    from app.config import settings
    return f"🎨 Pizarra: {title}\n{settings.public_url.rstrip('/')}/whiteboards/{board_id}"


def _whiteboard_link(text: str | None) -> dict | None:
    m = _WHITEBOARD_MSG_RE.fullmatch((text or "").strip())
    if not m:
        return None
    return {"title": m.group("title"), "url": f"/whiteboards/{m.group('id')}"}


templates.env.filters["whiteboard_link"] = _whiteboard_link


# Documentos que consultó MAX al responder en el chat (app/assistant/agent.py):
# van al final del mensaje, uno por línea, con URL absoluta para que se lean en
# avisos y reenvíos. En el feed se dibujan como botones bajo la respuesta.
ASSISTANT_SOURCES_HEADER = "📄 Documentos:"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_SOURCE_LINE = r"- [^\n]+?: https?://\S+/documents/" + _UUID + r"/view"
_SOURCES_BLOCK_RE = re.compile(
    r"\n\n" + re.escape(ASSISTANT_SOURCES_HEADER) + r"((?:\n" + _SOURCE_LINE + r")+)\Z"
)
_SOURCE_LINE_RE = re.compile(
    r"- (?P<title>[^\n]+?): https?://\S+/documents/(?P<id>" + _UUID + r")/view"
)


def assistant_sources_block(sources: list[dict]) -> str:
    """Bloque que agrega MAX al final de su respuesta en el chat."""
    from app.config import settings
    if not sources:
        return ""
    base = settings.public_url.rstrip("/")
    lines = [
        f"- {' '.join(str(s['title']).split())}: {base}/documents/{s['id']}/view"
        for s in sources
    ]
    return "\n\n" + ASSISTANT_SOURCES_HEADER + "\n" + "\n".join(lines)


def split_assistant_sources(text: str | None) -> tuple[str, list[dict]]:
    """(texto sin el bloque, [{"id", "title", "url"}]); sin bloque, (texto, [])."""
    text = text or ""
    m = _SOURCES_BLOCK_RE.search(text)
    if not m:
        return text, []
    sources = [
        {"id": x.group("id"), "title": x.group("title"), "url": f"/documents/{x.group('id')}/view"}
        for x in _SOURCE_LINE_RE.finditer(m.group(1))
    ]
    return text[:m.start()], sources


templates.env.filters["assistant_sources"] = split_assistant_sources


def _msg_preview(msg) -> str:
    """Texto corto de un mensaje para la lista de chats (no la URL cruda de un
    sticker o una ubicación)."""
    if getattr(msg, "deleted_at", None):
        return "Mensaje eliminado"
    content = msg.content or ""
    if _sticker_url(content):
        return "Sticker"
    if _osm_location(content):
        return "📍 Ubicación"
    board = _whiteboard_link(content)
    if board:
        return f"🎨 Pizarra: {board['title']}"
    return content or "📎 Archivo adjunto"


def _highlight(text: str | None, term: str | None) -> Markup:
    """Escapa `text` y marca cada aparición de `term` (sin distinguir mayúsculas)."""
    escaped = str(escape(text or ""))
    term = str(escape((term or "").strip()))
    if not term:
        return Markup(escaped)
    return Markup(re.sub(re.escape(term), lambda m: f'<mark class="bg-yellow-200 rounded px-0.5">{m.group(0)}</mark>',
                         escaped, flags=re.IGNORECASE))


templates.env.filters["msg_preview"] = _msg_preview
templates.env.filters["highlight"] = _highlight
templates.env.globals["stickers_enabled"] = _stickers_enabled


def _mentionify(html, names=None, me: str = "", everyone: bool = True) -> Markup:
    # Después de linkify: resalta @Nombre/@todos de los participantes del grupo
    # (en directos, solo @MAX y la mención de vuelta; sin @todos).
    from app.mentions import highlight
    return highlight(html, names or [], me, everyone)


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
