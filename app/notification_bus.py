"""Publica notificaciones al servicio central (notificationservice) por RabbitMQ.

notificationservice las persiste y las concentra con las de los demás dominios
en HossExtension. Contrato del mensaje (exchange topic `notifications`,
routing key `<source>.<type>`):

    {id, source, type, recipients: [corporate_id], title, body, url, data, occurred_at}

Los chats van aparte, al exchange topic `messages` (cola propia en
notificationservice, que guarda una entrada por conversación con su contador de
no leídos). Tipos: `message` (nuevo mensaje, recipients = los demás
participantes), `read` (recipients = quien lo leyó) y `deleted`.

Best-effort: el publish corre en un hilo de fondo con su
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
MESSAGES_EXCHANGE = "messages"

_queue: queue.Queue = queue.Queue(maxsize=1000)
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def publish(user_id, kind: str, title: str, body: str = "", url: str = "/", data: dict | None = None) -> None:
    """Encola la notificación para el usuario local `user_id`. Usuarios sin
    corporate_id (cuentas solo locales, sin SSO) no existen fuera de knowledge
    y se omiten."""
    if not settings.rabbitmq_url or not user_id:
        return
    recipients = _corporate_ids([user_id])
    if not recipients:
        return
    _enqueue(EXCHANGE, {
        "id": str(uuid.uuid4()),
        "type": kind,
        "recipients": recipients,
        "title": title,
        "body": body,
        "url": _absolute(url),
        "data": data,
    })


def publish_message(conversation_id, conversation_name: str | None, sender, recipient_ids,
                    message_id, body: str, url: str) -> None:
    """Nuevo mensaje de chat. `conversation_name` solo para grupos: en los
    directos el título de la conversación es el remitente."""
    if not settings.rabbitmq_url or not sender.corporate_id:
        return
    recipients = _corporate_ids(recipient_ids)
    if not recipients:
        return
    _enqueue(MESSAGES_EXCHANGE, {
        "id": str(message_id),
        "type": "message",
        "conversation_id": str(conversation_id),
        "conversation_name": conversation_name or "",
        "sender": {"corporate_id": str(sender.corporate_id), "name": sender.name or sender.email},
        "recipients": recipients,
        "body": body,
        "url": _absolute(url),
    })


def publish_read(conversation_id, user_id) -> None:
    if not settings.rabbitmq_url:
        return
    recipients = _corporate_ids([user_id])
    if not recipients:
        return
    _enqueue(MESSAGES_EXCHANGE, {
        "id": str(uuid.uuid4()),
        "type": "read",
        "conversation_id": str(conversation_id),
        "recipients": recipients,
    })


def publish_deleted(conversation_id) -> None:
    if not settings.rabbitmq_url:
        return
    _enqueue(MESSAGES_EXCHANGE, {
        "id": str(uuid.uuid4()),
        "type": "deleted",
        "conversation_id": str(conversation_id),
    })


def _absolute(url: str) -> str:
    return url if url.startswith("http") else settings.public_url.rstrip("/") + url


def _enqueue(exchange: str, event: dict) -> None:
    event["source"] = SOURCE
    event["occurred_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        _queue.put_nowait((exchange, event))
    except queue.Full:
        logger.warning("notification_bus: cola llena, se descarta %s/%s", exchange, event["type"])
        return
    _ensure_worker()


def _corporate_ids(user_ids) -> list[str]:
    from app.database import SessionLocal
    from app.models import User

    ids = [i for i in user_ids if i]
    if not ids:
        return []
    db = SessionLocal()
    try:
        rows = db.query(User.corporate_id).filter(User.id.in_(ids), User.corporate_id.isnot(None)).all()
        return [str(r[0]) for r in rows if r[0]]
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
            exchange, event = _queue.get(timeout=10)
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
                    for name in (EXCHANGE, MESSAGES_EXCHANGE):
                        channel.exchange_declare(name, exchange_type="topic", durable=True)
                channel.basic_publish(
                    exchange=exchange,
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
