"""Materialización de plantillas de tareas recurrentes.

Un `RecurringTask` es una plantilla que el encargado de un área predefine con una
frecuencia (diario / semanal / mensual). Esta capa convierte cada plantilla que
"toca hoy" en una `Task` real reutilizando todo el flujo existente de tareas.

La operación es idempotente: `RecurringTask.last_generated_on` evita duplicar la
tarea del día aunque el generador corra varias veces o el server reinicie.
"""
import calendar
import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from app import audit
from app.config import settings
from app.database import SessionLocal
from app.messaging import realtime
from app.models import (
    RecurringTask, Task, TaskShare, TaskStatusHistory,
    FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY, FREQ_CUSTOM, RECURRENCE_FREQUENCIES,
    TASK_PENDING,
)

logger = logging.getLogger(__name__)


def local_now() -> datetime:
    """Hora actual en la zona del negocio (settings.local_utc_offset_hours)."""
    return datetime.now(timezone(timedelta(hours=settings.local_utc_offset_hours)))


def parse_recurrence(frequency: str, day_of_week: str, day_of_month: str, days_of_week: list[str] | None = None):
    """Normaliza y valida la frecuencia. Devuelve (frequency, dow, dom, custom_days_csv).

    La comparten las tareas recurrentes y los eventos repetidos del calendario."""
    if frequency not in RECURRENCE_FREQUENCIES:
        raise HTTPException(400, "Frecuencia inválida")
    dow = dom = custom_days = None
    if frequency == FREQ_WEEKLY:
        try:
            dow = int(day_of_week)
        except (TypeError, ValueError):
            raise HTTPException(400, "Día de la semana inválido")
        if not 0 <= dow <= 6:
            raise HTTPException(400, "Día de la semana fuera de rango")
    elif frequency == FREQ_MONTHLY:
        try:
            dom = int(day_of_month)
        except (TypeError, ValueError):
            raise HTTPException(400, "Día del mes inválido")
        if not 1 <= dom <= 31:
            raise HTTPException(400, "Día del mes fuera de rango")
    elif frequency == FREQ_CUSTOM:
        try:
            days = {int(d) for d in (days_of_week or [])}
        except (TypeError, ValueError):
            raise HTTPException(400, "Días de la semana inválidos")
        if not days or not days.issubset(set(range(7))):
            raise HTTPException(400, "Elige al menos un día válido de la semana")
        custom_days = ",".join(str(d) for d in sorted(days))
    return frequency, dow, dom, custom_days


def matches_recurrence(
    frequency: str, day_of_week: int | None, day_of_month: int | None, days_of_week: str | None, day: date,
) -> bool:
    """¿`day` cae en la frecuencia? (sin contar inicio/fin ni estado)."""
    if frequency == FREQ_DAILY:
        return True
    if frequency == FREQ_WEEKLY:
        return day.weekday() == day_of_week
    if frequency == FREQ_CUSTOM:
        days = {int(d) for d in (days_of_week or "").split(",") if d != ""}
        return day.weekday() in days
    if frequency == FREQ_MONTHLY:
        last_day = calendar.monthrange(day.year, day.month)[1]
        # day_of_month puede exceder los días del mes (p.ej. 31 en febrero):
        # en ese caso cae en el último día del mes.
        target = min(day_of_month or 1, last_day)
        return day.day == target
    return False


def _due_today(rt: RecurringTask, today: date) -> bool:
    """¿La plantilla debe generar una tarea hoy?"""
    if not rt.is_active:
        return False
    if rt.start_date and today < rt.start_date:
        return False
    if rt.end_date and today > rt.end_date:
        return False
    if rt.last_generated_on == today:
        return False  # ya se generó hoy
    return matches_recurrence(rt.frequency, rt.day_of_week, rt.day_of_month, rt.days_of_week, today)


def generate_due_tasks(today: date | None = None) -> int:
    """Crea las `Task` de todas las plantillas activas que tocan hoy.

    Devuelve el número de tareas creadas. Seguro de correr varias veces al día.
    """
    today = today or local_now().date()
    db = SessionLocal()
    created = 0
    shared_notices = []  # (user_id, task_id, title): avisos tras el commit
    try:
        templates = db.query(RecurringTask).filter(RecurringTask.is_active == True).all()
        for rt in templates:
            if not _due_today(rt, today):
                continue
            task = Task(
                id=uuid.uuid4(),
                title=rt.title,
                description=rt.description,
                status=TASK_PENDING,
                priority=rt.priority,
                is_recurring=True,
                project_id=rt.project_id,
                # Sin depto en la plantilla, hereda el del asignado para que el admin del área la vea.
                department_id=rt.department_id or (rt.assignee.department_id if rt.assignee else None),
                assigned_to=rt.assigned_to,
                document_id=rt.document_id,
                created_by=rt.created_by,
                due_date=today,
            )
            db.add(task)
            db.flush()
            db.add(TaskStatusHistory(
                id=uuid.uuid4(),
                task_id=task.id,
                from_status=None,
                to_status=task.status,
                changed_by=rt.created_by,
                changed_at=task.created_at,
            ))
            # Los compartidos (activos) de la plantilla siguen cada tarea generada.
            for share in rt.shares:
                if share.user_id not in (task.assigned_to, task.created_by) and share.user and share.user.is_active:
                    db.add(TaskShare(task_id=task.id, user_id=share.user_id, shared_by=rt.created_by))
                    shared_notices.append((share.user_id, task.id, task.title))
            rt.last_generated_on = today
            created += 1
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("recurring task generation failed")
        raise
    finally:
        db.close()

    for user_id, task_id, title in shared_notices:
        realtime.notify_user(
            user_id, title="Tarea recurrente compartida contigo", body=title,
            url=f"/tasks/{task_id}", kind="task_shared",
        )
    if created:
        realtime.notify_tasks()
        audit.log_action(
            "recurring_generate",
            resource_type="recurring_task",
            details=f"generated={created} date={today.isoformat()}",
        )
        logger.info("recurring tasks: generated %d for %s", created, today)
    return created
