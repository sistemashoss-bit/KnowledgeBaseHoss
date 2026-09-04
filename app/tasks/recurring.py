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
from datetime import date

from app import audit
from app.database import SessionLocal
from app.models import (
    RecurringTask, Task,
    FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY,
    TASK_PENDING,
)

logger = logging.getLogger(__name__)


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

    if rt.frequency == FREQ_DAILY:
        return True
    if rt.frequency == FREQ_WEEKLY:
        return today.weekday() == rt.day_of_week
    if rt.frequency == FREQ_MONTHLY:
        last_day = calendar.monthrange(today.year, today.month)[1]
        # day_of_month puede exceder los días del mes (p.ej. 31 en febrero):
        # en ese caso la tarea cae en el último día del mes.
        target = min(rt.day_of_month or 1, last_day)
        return today.day == target
    return False


def generate_due_tasks(today: date | None = None) -> int:
    """Crea las `Task` de todas las plantillas activas que tocan hoy.

    Devuelve el número de tareas creadas. Seguro de correr varias veces al día.
    """
    today = today or date.today()
    db = SessionLocal()
    created = 0
    try:
        templates = db.query(RecurringTask).filter(RecurringTask.is_active == True).all()
        for rt in templates:
            if not _due_today(rt, today):
                continue
            db.add(Task(
                id=uuid.uuid4(),
                title=rt.title,
                description=rt.description,
                status=TASK_PENDING,
                priority=rt.priority,
                is_recurring=True,
                project_id=rt.project_id,
                department_id=rt.department_id,
                assigned_to=rt.assigned_to,
                document_id=rt.document_id,
                created_by=rt.created_by,
                due_date=today,
            ))
            rt.last_generated_on = today
            created += 1
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("recurring task generation failed")
        raise
    finally:
        db.close()

    if created:
        audit.log_action(
            "recurring_generate",
            resource_type="recurring_task",
            details=f"generated={created} date={today.isoformat()}",
        )
        logger.info("recurring tasks: generated %d for %s", created, today)
    return created
