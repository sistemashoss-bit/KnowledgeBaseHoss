"""Recordatorios del calendario y avisos de tareas por vencer.

Un solo ciclo de fondo que corre cada minuto (ver `reminder_scheduler_loop`,
lanzado desde el lifespan de main.py):

- Citas: avisa al dueño cuando llega `CalendarEvent.remind_at`.
- Tareas: a partir de settings.task_due_reminder_hour_local avisa al asignado
  de las tareas que vencen hoy o mañana (una vez por día).

Cada envío se "reclama" con un UPDATE ... RETURNING antes de notificar, así que
aunque corran varias instancias a la vez (o el server reinicie) nadie recibe el
mismo aviso dos veces, sin depender de Valkey.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import or_, update

from app.config import settings
from app.database import SessionLocal
from app.messaging import realtime
from app.models import CALENDAR_ALL_DAY_REMINDER_HOUR, CalendarEvent, Task, TASK_DONE

logger = logging.getLogger(__name__)

# Un recordatorio que se pasó por más de esto (server caído, evento creado
# tarde) ya no se envía: avisar de una cita que ya pasó solo estorba.
STALE_AFTER = timedelta(hours=1)
POLL_SECONDS = 60

_WEEKDAYS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _offset() -> timedelta:
    return timedelta(hours=settings.local_utc_offset_hours)


def to_local(dt_utc: datetime) -> datetime:
    """UTC naive → hora local del negocio, naive."""
    return dt_utc + _offset()


def to_utc(dt_local: datetime) -> datetime:
    """Hora local del negocio, naive → UTC naive."""
    return dt_local - _offset()


def compute_remind_at(start_at: datetime, all_day: bool, minutes: int | None) -> datetime | None:
    """Momento (UTC) en que se envía el recordatorio de un evento.

    En eventos de todo el día los minutos cuentan en días completos y el aviso
    llega a CALENDAR_ALL_DAY_REMINDER_HOUR hora local, no a medianoche."""
    if minutes is None:
        return None
    if not all_day:
        return start_at - timedelta(minutes=minutes)
    day = to_local(start_at).date() - timedelta(days=minutes // 1440)
    return to_utc(datetime.combine(day, datetime.min.time()) + timedelta(hours=CALENDAR_ALL_DAY_REMINDER_HOUR))


def _when_label(ev_start_local: datetime, all_day: bool, today: date) -> str:
    d = ev_start_local.date()
    if d == today:
        day = "Hoy"
    elif d == today + timedelta(days=1):
        day = "Mañana"
    else:
        day = f"{_WEEKDAYS[d.weekday()]} {d.day:02d}/{d.month:02d}"
    return f"{day} (todo el día)" if all_day else f"{day} a las {ev_start_local:%H:%M}"


def send_event_reminders(now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    db = SessionLocal()
    try:
        rows = db.execute(
            update(CalendarEvent)
            .where(
                CalendarEvent.reminded_at.is_(None),
                CalendarEvent.remind_at.isnot(None),
                CalendarEvent.remind_at <= now,
                CalendarEvent.remind_at > now - STALE_AFTER,
            )
            .values(reminded_at=now)
            .returning(
                CalendarEvent.owner_id, CalendarEvent.title, CalendarEvent.start_at,
                CalendarEvent.all_day, CalendarEvent.location,
            )
        ).all()
        db.commit()
    finally:
        db.close()

    today = to_local(now).date()
    for owner_id, title, start_at, all_day, location in rows:
        start_local = to_local(start_at)
        body = _when_label(start_local, all_day, today)
        if location:
            body += f" · {location}"
        realtime.notify_user(
            owner_id,
            title=f"Recordatorio: {title}",
            body=body,
            url=f"/calendar/?date={start_local.date().isoformat()}",
            kind="calendar_reminder",
        )
    return len(rows)


def send_task_due_reminders(now: datetime | None = None) -> int:
    local = to_local(now or datetime.utcnow())
    if local.hour < settings.task_due_reminder_hour_local:
        return 0
    today = local.date()
    tomorrow = today + timedelta(days=1)

    db = SessionLocal()
    try:
        rows = db.execute(
            update(Task)
            .where(
                Task.assigned_to.isnot(None),
                Task.status != TASK_DONE,
                Task.archived_at.is_(None),
                Task.due_date.in_((today, tomorrow)),
                or_(Task.due_reminded_on.is_(None), Task.due_reminded_on < today),
            )
            .values(due_reminded_on=today)
            .returning(Task.id, Task.title, Task.assigned_to, Task.due_date)
            .execution_options(synchronize_session=False)
        ).all()
        db.commit()
    finally:
        db.close()

    for task_id, title, assigned_to, due_date in rows:
        realtime.notify_user(
            assigned_to,
            title="Tu tarea vence hoy" if due_date == today else "Tu tarea vence mañana",
            body=title,
            url=f"/tasks/{task_id}",
            kind="task_due",
        )
    return len(rows)


def run_reminders() -> tuple[int, int]:
    return send_event_reminders(), send_task_due_reminders()


async def reminder_scheduler_loop() -> None:
    """Revisa recordatorios cada minuto. Never raises out."""
    while True:
        try:
            events, tasks = await asyncio.to_thread(run_reminders)
            if events or tasks:
                logger.info("reminders sent: events=%s tasks=%s", events, tasks)
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminder scheduler failed")
            await asyncio.sleep(POLL_SECONDS * 5)
