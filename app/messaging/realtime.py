"""Real-time chat delivery over Redis Pub/Sub, streamed to the browser as SSE.

Postgres stays the source of truth; Redis only carries a lightweight "new
message" signal so clients refresh instantly instead of polling every 3s. If
Redis is unavailable the SSE stream degrades to keepalives and the template's
slow fallback poll keeps things working.
"""
import asyncio
import json
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_KEEPALIVE = 25  # seconds — comment ping to keep the connection alive
_aclient = None


async def _aget():
    """Lazily build (and memoize) an async Redis client, or None if unavailable."""
    global _aclient
    if _aclient is not None:
        return _aclient
    if not settings.valkey_url:
        return None
    try:
        import redis.asyncio as aredis

        url = settings.valkey_url.replace("valkeys://", "rediss://", 1)
        _aclient = aredis.from_url(url, decode_responses=True)
        await _aclient.ping()
    except Exception as exc:
        logger.warning("async Valkey unavailable: %s — realtime disabled", exc)
        _aclient = None
    return _aclient


def channel(conv_id) -> str:
    return f"chat:conv:{conv_id}"


def user_channel(user_id) -> str:
    """Per-user signalling channel (call rings, etc.), independent of any conversation."""
    return f"user:{user_id}:signals"


def notify_user(user_id) -> None:
    """Push a lightweight 'refresh your notifications' signal to a user.

    The client re-fetches /api/notifications on receipt. No-op (falls back to the
    client's slow poll) when Valkey is unavailable.
    """
    from app import valkey_client as vk
    vk.publish(user_channel(user_id), json.dumps({"type": "notify"}))


async def _channel_stream(chan: str, request, event_name: str):
    """Yield SSE frames from a Pub/Sub channel until the client disconnects.

    Each published payload is forwarded as `event: <event_name>` with the raw
    payload as data. Degrades to keepalives when Redis is unavailable.
    """
    yield ": connected\n\n"

    r = await _aget()
    if r is None:
        # No Redis: emit keepalives; the client falls back to slow polling.
        while not await request.is_disconnected():
            await asyncio.sleep(_KEEPALIVE)
            yield ": keepalive\n\n"
        return

    pubsub = r.pubsub()
    await pubsub.subscribe(chan)
    try:
        while not await request.is_disconnected():
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_KEEPALIVE)
            if msg is None:
                yield ": keepalive\n\n"
                continue
            data = msg.get("data", "new")
            yield f"event: {event_name}\ndata: {data}\n\n"
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await pubsub.unsubscribe(chan)
            await pubsub.aclose()
        except Exception:
            pass


async def event_stream(conv_id: str, request):
    """Yield SSE frames for a conversation until the client disconnects."""
    async for frame in _channel_stream(channel(conv_id), request, "message"):
        yield frame


async def user_event_stream(user_id: str, request):
    """Yield SSE frames on the user's personal channel (call rings, etc.)."""
    async for frame in _channel_stream(user_channel(user_id), request, "signal"):
        yield frame
