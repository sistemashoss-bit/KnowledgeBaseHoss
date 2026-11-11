"""
Valkey (Redis-compatible) client.
Used for: RAG response cache + user presence.
Falls back gracefully if VALKEY_URL is not configured.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
_client = None


def _get():
    global _client
    if _client is not None:
        return _client
    from app.config import settings
    if not settings.valkey_url:
        return None
    try:
        import redis
        url = settings.valkey_url.replace("valkeys://", "rediss://", 1)
        _client = redis.from_url(url, decode_responses=True, socket_timeout=2)
        _client.ping()
        _time_commands(_client)
    except Exception as exc:
        logger.warning("Valkey unavailable: %s — caching disabled", exc)
        _client = None
    return _client


def _time_commands(client) -> None:
    """Cada comando suma a `vk` en el Server-Timing de la petición (app.timing)."""
    from app import timing
    raw = client.execute_command

    def timed(*args, **kwargs):
        with timing.span("vk"):
            return raw(*args, **kwargs)

    client.execute_command = timed


def available() -> bool:
    """True if a Redis/Valkey connection is usable."""
    return _get() is not None


def publish(channel: str, message: str = "new") -> None:
    """Best-effort publish for real-time fan-out. Never raises."""
    r = _get()
    if r is None:
        return
    try:
        r.publish(channel, message)
    except Exception:
        pass


# ── Rate limiting genérico (ventana fija) ─────────────────────────────────────

def hit_rate_limit(key: str, limit: int, window: int) -> bool:
    """Cuenta un intento en `key` y devuelve True si ya pasó de `limit` dentro
    de `window` segundos. Sin Valkey no limita."""
    r = _get()
    if r is None:
        return False
    try:
        count = r.incr(f"rl:{key}")
        if count == 1:
            r.expire(f"rl:{key}", window)
        return int(count) > limit
    except Exception:
        return False


# ── RAG response cache ────────────────────────────────────────────────────────

_RAG_TTL = 600  # 10 minutes


def _rag_key(query: str, role: str, dept_id: str | None, user_id: str | None) -> str:
    # user_id entra a la clave porque la visibilidad 'custom' (personas específicas)
    # ya no es uniforme por role+dept_id: dos empleados del mismo depto pueden tener
    # acceso distinto a documentos 'custom', y compartir cache filtraría/ocultaría
    # contenido entre ellos.
    raw = f"{role}:{dept_id}:{user_id}:{query}"
    return "rag:" + hashlib.sha256(raw.encode()).hexdigest()


def get_cached_rag(query: str, role: str, dept_id: str | None, user_id: str | None = None) -> str | None:
    r = _get()
    if r is None:
        return None
    try:
        return r.get(_rag_key(query, role, dept_id, user_id))
    except Exception:
        return None


def cache_rag(query: str, role: str, dept_id: str | None, response: str, user_id: str | None = None) -> None:
    r = _get()
    if r is None:
        return
    try:
        r.setex(_rag_key(query, role, dept_id, user_id), _RAG_TTL, response)
    except Exception:
        pass


# ── Caché JSON genérica ───────────────────────────────────────────────────────

def get_json(key: str):
    """Valor cacheado (ya decodificado) o None. Sin Valkey siempre None."""
    r = _get()
    if r is None:
        return None
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def set_json(key: str, value, ttl: int) -> None:
    r = _get()
    if r is None:
        return
    try:
        r.set(key, json.dumps(value), ex=ttl)
    except Exception:
        pass


# ── Lock con TTL ──────────────────────────────────────────────────────────────

def acquire_lock(key: str, ttl: int) -> bool:
    """True si tomó el lock (SET NX con TTL). Sin Valkey devuelve True: no hay
    con qué coordinar y bloquear dejaría la función sin servicio."""
    r = _get()
    if r is None:
        return True
    try:
        return bool(r.set(key, "1", ex=ttl, nx=True))
    except Exception:
        return True


def release_lock(key: str) -> None:
    r = _get()
    if r is None:
        return
    try:
        r.delete(key)
    except Exception:
        pass


def set_flag(key: str, ttl: int) -> bool:
    """Marca `key` por `ttl` segundos; True solo si no estaba marcada (para
    avisar algo una sola vez por ventana). Sin Valkey, True."""
    return acquire_lock(key, ttl)


def ttl(key: str) -> int:
    """Segundos que le quedan a `key` (0 si no existe o sin Valkey)."""
    r = _get()
    if r is None:
        return 0
    try:
        value = r.ttl(key)
        return max(int(value), 0)
    except Exception:
        return 0


# ── Once-daily lock ───────────────────────────────────────────────────────────

def try_acquire_daily_lock(key: str) -> bool:
    """Returns True exactly once per 24 h for a given key. Use as a daily gate."""
    r = _get()
    if r is None:
        return False
    try:
        return bool(r.set(key, "1", ex=86400, nx=True))
    except Exception:
        return False


# ── Hoss SSO token (para llamadas server-to-server a hoss-api) ────────────────
# Guardamos el accessToken de hoss del staff al hacer SSO, para reusarlo en
# acciones que llaman a la API de hoss (p. ej. sincronizar sucursales/regiones).
# hoss valida su propia expiración al usarlo; el TTL aquí es solo un techo.
_HOSS_TOKEN_TTL = 8 * 3600  # 8h


def store_hoss_token(user_id, token: str) -> None:
    r = _get()
    if r is None or not token:
        return
    try:
        r.setex(f"hoss_token:{str(user_id)}", _HOSS_TOKEN_TTL, token)
    except Exception:
        pass


def get_hoss_token(user_id) -> str | None:
    r = _get()
    if r is None:
        return None
    try:
        return r.get(f"hoss_token:{str(user_id)}")
    except Exception:
        return None


# ── User presence ─────────────────────────────────────────────────────────────

_LASTSEEN_TTL = 86400 * 30   # 30 days
_PRESENCE_THROTTLE = 60      # write at most once per minute per user


def update_last_seen(user_id) -> None:
    r = _get()
    if r is None:
        return
    try:
        sid = str(user_id)
        if r.set(f"user:lsact:{sid}", "1", ex=_PRESENCE_THROTTLE, nx=True):
            r.setex(f"user:lastseen:{sid}", _LASTSEEN_TTL, datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


def get_last_seen(user_id) -> str | None:
    r = _get()
    if r is None:
        return None
    try:
        return r.get(f"user:lastseen:{str(user_id)}")
    except Exception:
        return None


def get_last_seen_many(user_ids: list) -> dict:
    """Returns {str(user_id): iso_str | None} for a list of user ids (single MGET)."""
    r = _get()
    if r is None:
        return {}
    try:
        ids = [str(i) for i in user_ids]
        values = r.mget([f"user:lastseen:{i}" for i in ids])
        return {ids[n]: values[n] for n in range(len(ids))}
    except Exception:
        return {}
