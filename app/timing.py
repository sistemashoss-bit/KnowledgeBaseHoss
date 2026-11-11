"""Medición por petición: cabecera `Server-Timing` y log de las lentas.

Cada respuesta HTTP (menos /assets y los streams SSE) lleva
`Server-Timing: db;dur=…;desc="N consultas", vk;dur=…, app;dur=…`, visible en
DevTools → Network → (petición) → Timing. Las que tardan más de
`settings.slow_request_ms` se registran como WARNING (el nivel que sale sin
configurar logging) con el mismo desglose.

Los endpoints síncronos corren en el threadpool con una COPIA del contexto: por
eso el contextvar guarda un objeto mutable que el hilo modifica en sitio.
"""
import contextvars
import logging
import time
from contextlib import contextmanager

from sqlalchemy import event

logger = logging.getLogger(__name__)

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("request_timing", default=None)


def add(name: str, seconds: float) -> None:
    rec = _current.get()
    if rec is None:
        return
    total, count = rec.get(name, (0.0, 0))
    rec[name] = (total + seconds, count + 1)


@contextmanager
def span(name: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        add(name, time.perf_counter() - start)


def instrument_engine(engine) -> None:
    """Suma a `db` el tiempo de cada consulta de este engine."""

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("timing_start", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        starts = conn.info.get("timing_start")
        if starts:
            add("db", time.perf_counter() - starts.pop())


def _header(rec: dict, total: float) -> str:
    parts = []
    for name, (secs, count) in sorted(rec.items()):
        parts.append(f'{name};dur={secs * 1000:.1f};desc="{count}x"')
    parts.append(f"app;dur={total * 1000:.1f}")
    return ", ".join(parts)


class TimingMiddleware:
    SKIP_PREFIXES = ("/assets",)

    def __init__(self, app):
        from app.config import settings

        self.app = app
        self.slow = settings.slow_request_ms / 1000

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(self.SKIP_PREFIXES):
            return await self.app(scope, receive, send)

        rec: dict = {}
        token = _current.set(rec)
        start = time.perf_counter()
        state = {"status": 0, "stream": False}

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                ctype = next((v for k, v in headers if k.lower() == b"content-type"), b"")
                state["status"] = message["status"]
                state["stream"] = ctype.startswith(b"text/event-stream")
                if not state["stream"]:
                    headers.append((b"server-timing", _header(rec, time.perf_counter() - start).encode()))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            _current.reset(token)
            total = time.perf_counter() - start
            if not state["stream"] and total >= self.slow:
                logger.warning(
                    "lento %s %s %s %s",
                    scope["method"], scope["path"], state["status"], _header(rec, total),
                )
