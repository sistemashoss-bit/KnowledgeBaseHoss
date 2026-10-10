"""Publica notificaciones al servicio central (notificationservice) por RabbitMQ.

notificationservice las persiste y las concentra con las de los demás dominios
en HossExtension. Contrato del mensaje (exchange topic `notifications`,
routing key `<source>.<type>`):

    {id, source, type, recipients: [corporate_id], title, body, url, data, occurred_at}

Best-effort, igual que app.push: el publish corre en un hilo de fondo con su
propia conexión (pika no es thread-safe) y cualquier falla se registra y se
descarta — una notificación perdida nunca debe romper el request que la generó.
Sin RABBITMQ_URL configurado es un no-op.
"""
import json
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from app.config import settings

logger = logging.getLogger(__name__)

SOURCE = "knowledge"
EXCHANGE = "notifications"

_queue: queue.Queue = queue.Queue(maxsize=1000)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def publish(user_id, kind: str, title: str, body: str = "", url: str = "/", data: dict | None = None) -> None:
    """Encola la notificación para el usuario local `user_id`. Usuarios sin
    corporate_id (cuentas solo locales, sin SSO) no existen fuera de knowledge
    y se omiten."""
    if not settings.rabbitmq_url or not user_id:
        return
    corporate_id = _corporate_id(user_id)
    if not corporate_id:
        return
    event = {
        "id": str(uuid.uuid4()),
        "source": SOURCE,
        "type": kind,
        "recipients": [corporate_id],
        "title": title,
        "body": body,
        "url": url if url.startswith("http") else settings.public_url.rstrip("/") + url,
        "data": data,
        "occurred_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        _queue.put_nowait(event)
    except queue.Full:
        logger.warning("notification_bus: cola llena, se descarta %s", kind)
        return
    _ensure_worker()


def _corporate_id(user_id) -> str | None:
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        row = db.query(User.corporate_id).filter(User.id == user_id).first()
        return str(row[0]) if row and row[0] else None
    finally:
        db.close()


def _ensure_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="notification-bus", daemon=True)
            _worker.start()


def _run() -> None:
    import pika

    params = pika.URLParameters(settings.rabbitmq_url)
    conn = channel = None
    while True:
        try:
            event = _queue.get(timeout=10)
        except queue.Empty:
            # Ociosa: atender heartbeats para que RabbitMQ no cierre la conexión.
            try:
                if conn is not None and conn.is_open:
                    conn.process_data_events(time_limit=0)
            except Exception:
                conn = channel = None
            continue
        for attempt in range(2):
            try:
                if conn is None or conn.is_closed:
                    conn = pika.BlockingConnection(params)
                    channel = conn.channel()
                    channel.exchange_declare(EXCHANGE, exchange_type="topic", durable=True)
                channel.basic_publish(
                    exchange=EXCHANGE,
                    routing_key=f"{SOURCE}.{event['type']}",
                    body=json.dumps(event),
                    properties=pika.BasicProperties(
                        content_type="application/json",
                        delivery_mode=pika.DeliveryMode.Persistent,
                        message_id=event["id"],
                    ),
                )
                break
            except Exception as exc:
                # Conexión vieja (heartbeat vencido mientras estaba ociosa): se
                # reconecta una vez antes de descartar el evento.
                logger.warning("notification_bus: falló publish (%s), intento %d", exc, attempt + 1)
                try:
                    if conn is not None and conn.is_open:
                        conn.close()
                except Exception:
                    pass
                conn = channel = None
                if attempt == 0:
                    time.sleep(1)
