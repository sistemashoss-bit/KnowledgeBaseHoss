"""Recordatorios del calendario y avisos de tareas por vencer.

Un solo ciclo de fondo que corre cada minuto (ver `reminder_scheduler_loop`,
lanzado desde el lifespan de main.py):

- Citas: avisa al dueño (y a con quienes lo compartió) cuando llega
  `CalendarEvent.remind_at`. En las que se
  repiten, al avisar (o si el aviso se pasó) `remind_at` avanza a la siguiente
  ocurrencia en la misma transacción.
- Seguimientos de tareas: avisa al responsable (asignado, o el creador si la
  tarea no tiene persona) cuando llega `TaskFollowUp.remind_at`, salvo que el
  seguimiento ya esté hecho o la tarea terminada/archivada.
- Tareas: a partir de settings.task_due_reminder_hour_local avisa al asignado
  de las tareas que vencen hoy o mañana (una vez por día).

Cada envío se "reclama" con un UPDATE ... RETURNING antes de notificar, así que
aunque corran varias instancias a la vez (o el server reinicie) nadie recibe el
mismo aviso dos veces, sin depender de Valkey.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import or_, select, update

from app.config import settings
from app.database import SessionLocal
from app.messaging import realtime
from app.models import CALENDAR_ALL_DAY_REMINDER_HOUR, CalendarEvent, Task, TaskFollowUp, TASK_DONE
from app.tasks.recurring import matches_recurrence

logger = logging.getLogger(__name__)

# Un recordatorio que se pasó por más de esto (server caído, evento creado
# tarde) ya no se envía: avisar de una cita que ya pasó solo estorba.
STALE_AFTER = timedelta(hours=1)
POLL_SECONDS = 60
# Tope de días que se recorren buscando la siguiente ocurrencia (cubre un
# mensual con día 31 y cualquier otra frecuencia con holgura).
MAX_SCAN_DAYS = 800

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


def iter_occurrences(ev: CalendarEvent, from_day: date, to_day: date | None = None):
    """Inicios (UTC) de las ocurrencias de `ev` cuyo día local está entre
    `from_day` y `to_day` (incluidos). Un evento sin repetición tiene una sola."""
    start_local = to_local(ev.start_at)
    if not ev.frequency:
        if from_day <= start_local.date() and (to_day is None or start_local.date() <= to_day):
            yield ev.start_at
        return
    day = max(start_local.date(), from_day)
    last = min(d for d in (ev.repeat_until, to_day, day + timedelta(days=MAX_SCAN_DAYS)) if d)
    while day <= last:
        if matches_recurrence(ev.frequency, ev.day_of_week, ev.day_of_month, ev.days_of_week, day):
            yield to_utc(datetime.combine(day, start_local.time()))
        day += timedelta(days=1)


def next_remind_at(ev: CalendarEvent, after: datetime) -> datetime | None:
    """Recordatorio de un evento: el de su inicio o, si se repite, el de la
    primera ocurrencia cuyo aviso cae después de `after` (UTC)."""
    if ev.reminder_minutes is None:
        return None
    if not ev.frequency:
        return compute_remind_at(ev.start_at, ev.all_day, ev.reminder_minutes)
    for start in iter_occurrences(ev, to_local(after).date()):
        remind = compute_remind_at(start, ev.all_day, ev.reminder_minutes)
        if remind > after:
            return remind
    return None


def _occurrence_for(ev: CalendarEvent, remind_at: datetime) -> datetime:
    """Inicio de la ocurrencia a la que corresponde el aviso `remind_at`."""
    for start in iter_occurrences(ev, to_local(remind_at).date()):
        if compute_remind_at(start, ev.all_day, ev.reminder_minutes) >= remind_at:
            return start
    return ev.start_at


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
        claimed = db.execute(
            update(CalendarEvent)
            .where(
                CalendarEvent.reminded_at.is_(None),
                CalendarEvent.remind_at.isnot(None),
                CalendarEvent.remind_at <= now,
                CalendarEvent.remind_at > now - STALE_AFTER,
            )
            .values(reminded_at=now)
            .returning(CalendarEvent.id, CalendarEvent.remind_at)
            .execution_options(synchronize_session=False)
        ).all()
        sent_remind = dict(claimed)

        rows = []
        if sent_remind:
            for ev in db.query(CalendarEvent).filter(CalendarEvent.id.in_(list(sent_remind))).all():
                remind_at = sent_remind[ev.id]
                start_at = _occurrence_for(ev, remind_at) if ev.frequency else ev.start_at
                recipients = [ev.owner_id] + [sh.user_id for sh in ev.shares]
                rows.append((recipients, ev.id, ev.title, start_at, ev.all_day, ev.location))
                if ev.frequency:
                    ev.remind_at = next_remind_at(ev, remind_at)
                    ev.reminded_at = None

        # Repetidos cuyo aviso se pasó sin enviarse (server caído): saltan a la
        # siguiente ocurrencia en vez de quedarse atorados.
        stale = (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.frequency.isnot(None),
                CalendarEvent.reminded_at.is_(None),
                CalendarEvent.remind_at <= now - STALE_AFTER,
            )
            .with_for_update(skip_locked=True)
            .all()
        )
        for ev in stale:
            ev.remind_at = next_remind_at(ev, now - STALE_AFTER)
        db.commit()
    finally:
        db.close()

    today = to_local(now).date()
    for recipients, event_id, title, start_at, all_day, location in rows:
        start_local = to_local(start_at)
        body = _when_label(start_local, all_day, today)
        if location:
            body += f" · {location}"
        for user_id in recipients:
            realtime.notify_user(
                user_id,
                title=f"Recordatorio: {title}",
                body=body,
                url=f"/calendar/?date={start_local.date().isoformat()}&event={event_id}",
                kind="calendar_reminder",
            )
    return len(rows)


def send_follow_up_reminders(now: datetime | None = None) -> int:
    now = now or datetime.utcnow()
    open_tasks = select(Task.id).where(Task.status != TASK_DONE, Task.archived_at.is_(None))
    db = SessionLocal()
    try:
        claimed = db.execute(
            update(TaskFollowUp)
            .where(
                TaskFollowUp.reminded_at.is_(None),
                TaskFollowUp.remind_at.isnot(None),
                TaskFollowUp.remind_at <= now,
                TaskFollowUp.remind_at > now - STALE_AFTER,
                TaskFollowUp.done_at.is_(None),
                TaskFollowUp.task_id.in_(open_tasks),
            )
            .values(reminded_at=now)
            .returning(TaskFollowUp.task_id, TaskFollowUp.start_at, TaskFollowUp.end_at, TaskFollowUp.note)
            .execution_options(synchronize_session=False)
        ).all()
        tasks = {
            t.id: t for t in db.query(Task).filter(Task.id.in_({r.task_id for r in claimed})).all()
        } if claimed else {}
        rows = []
        for r in claimed:
            task = tasks.get(r.task_id)
            if task:
                rows.append((task.assigned_to or task.created_by, task.id, task.title, r.start_at, r.end_at, r.note))
        db.commit()
    finally:
        db.close()

    today = to_local(now).date()
    for owner_id, task_id, title, start_at, end_at, note in rows:
        start_local = to_local(start_at)
        body = f"{_when_label(start_local, False, today)}–{to_local(end_at):%H:%M}"
        if note:
            body += f" · {note}"
        realtime.notify_user(
            owner_id,
            title=f"Seguimiento: {title}",
            body=body,
            url=f"/tasks/{task_id}",
            kind="task_follow_up",
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


def run_reminders() -> tuple[int, int, int]:
    return send_event_reminders(), send_follow_up_reminders(), send_task_due_reminders()


async def reminder_scheduler_loop() -> None:
    """Revisa recordatorios cada minuto. Never raises out."""
    while True:
        try:
            events, follow_ups, tasks = await asyncio.to_thread(run_reminders)
            if events or follow_ups or tasks:
                logger.info("reminders sent: events=%s follow_ups=%s tasks=%s", events, follow_ups, tasks)
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("reminder scheduler failed")
            await asyncio.sleep(POLL_SECONDS * 5)
