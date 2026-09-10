"""Web Push (service worker) delivery, via VAPID.

Complements the existing Redis pub/sub signalling in app.messaging.realtime:
that one only reaches a tab that's open and subscribed to the SSE stream.
This module reaches the browser (or OS) even with every tab closed, through
the previously-registered service worker.

Best-effort like the rest of the realtime layer: sends happen in a background
thread so callers (sync or async routes alike) never block on network I/O,
and every failure is swallowed — a missed push must never break the request
that triggered it.
"""
import json
import logging
import threading

from app.config import settings

logger = logging.getLogger(__name__)


def configured() -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def send_to_user(user_id, title: str, body: str = "", url: str = "/") -> None:
    """Fire-and-forget: push `title`/`body` to every subscription this user
    has registered. No-op if VAPID isn't configured or the user has none."""
    if not configured() or not user_id:
        return
    threading.Thread(
        target=_send_all, args=(user_id, title, body, url), daemon=True,
    ).start()


def _send_all(user_id, title: str, body: str, url: str) -> None:
    from pywebpush import WebPushException, webpush
    from app.database import SessionLocal
    from app.models import PushSubscription

    payload = json.dumps({"title": title, "body": body, "url": url})
    db = SessionLocal()
    try:
        subs = db.query(PushSubscription).filter(PushSubscription.user_id == user_id).all()
        for sub in subs:
            try:
                webpush(
                    subscription_info={
                        "endpoint": sub.endpoint,
                        "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                    },
                    data=payload,
                    vapid_private_key=settings.vapid_private_key,
                    vapid_claims={"sub": f"mailto:{settings.vapid_admin_email}"},
                )
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    # Subscription expired or was revoked by the browser — forget it.
                    db.delete(sub)
                    db.commit()
                else:
                    logger.warning("web push failed (%s): %s", status, exc)
            except Exception:
                logger.exception("unexpected web push error")
    finally:
        db.close()
