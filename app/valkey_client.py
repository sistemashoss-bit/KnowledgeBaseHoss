"""
Valkey (Redis-compatible) client.
Used for: login rate limiting + RAG response cache + user presence.
Falls back gracefully if VALKEY_URL is not configured.
"""
import hashlib
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
    except Exception as exc:
        logger.warning("Valkey unavailable: %s — caching disabled", exc)
        _client = None
    return _client


# ── Login rate limiting ───────────────────────────────────────────────────────

_MAX_ATTEMPTS = 5
_WINDOW = 300  # 5 minutes


def is_rate_limited(email: str) -> bool:
    r = _get()
    if r is None:
        return False
    try:
        key = f"login_fail:{email.lower()}"
        count = r.get(key)
        return int(count or 0) >= _MAX_ATTEMPTS
    except Exception:
        return False


def record_login_failure(email: str) -> int:
    r = _get()
    if r is None:
        return 0
    try:
        key = f"login_fail:{email.lower()}"
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, _WINDOW)
        count, _ = pipe.execute()
        return int(count)
    except Exception:
        return 0


def clear_login_failures(email: str) -> None:
    r = _get()
    if r is None:
        return
    try:
        r.delete(f"login_fail:{email.lower()}")
    except Exception:
        pass


# ── RAG response cache ────────────────────────────────────────────────────────

_RAG_TTL = 600  # 10 minutes


def _rag_key(query: str, role: str, dept_id: str | None) -> str:
    raw = f"{role}:{dept_id}:{query}"
    return "rag:" + hashlib.sha256(raw.encode()).hexdigest()


def get_cached_rag(query: str, role: str, dept_id: str | None) -> str | None:
    r = _get()
    if r is None:
        return None
    try:
        return r.get(_rag_key(query, role, dept_id))
    except Exception:
        return None


def cache_rag(query: str, role: str, dept_id: str | None, response: str) -> None:
    r = _get()
    if r is None:
        return
    try:
        r.setex(_rag_key(query, role, dept_id), _RAG_TTL, response)
    except Exception:
        pass


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
