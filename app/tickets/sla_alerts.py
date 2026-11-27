"""Avisos de SLA vencido en tickets con categoría (PLAN_CATEGORIAS_TICKETS.md).

Un ciclo de fondo cada minuto, lanzado desde el lifespan de main.py. En los
tickets con categoría abiertos o en atención ("En espera" pausa el SLA), si se
venció el SLA de primera respuesta o el de resolución del paso actual, avisa a
quien el paso designa (`alert_target`):

- "boss": el jefe directo del responsable (todos los empatados en nivel); si el
  paso no tiene responsable o no tiene jefe, los admins del departamento del paso.
- "department": los admins del departamento elegido.

El ticket no cambia de paso: escalar es siempre a mano. Cada aviso queda en
ticket_sla_alerts (una vez por ticket, paso, métrica y persona, con
ON CONFLICT DO NOTHING, así que varias instancias no avisan dos veces) y le da a
esa persona permiso de ver el ticket.
"""
import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from app.database import SessionLocal
from app.messaging import realtime
from app.models import (
    ALERT_BOSS, ALERT_DEPARTMENT, SLA_METRICS, TICKET_IN_PROGRESS, TICKET_OPEN, Ticket, TicketCategory,
    TicketCategoryStep, TicketSlaAlert, User,
)
from app.tickets.hierarchy import direct_bosses
from app.tickets.service import department_admin_ids, sla_status

logger = logging.getLogger(__name__)

POLL_SECONDS = 60
METRIC_LABELS = {"response": "primera respuesta", "resolution": "resolución"}


def current_step(t: Ticket) -> TicketCategoryStep | None:
    if not t.category or t.step_position is None:
        return None
    return next((s for s in t.category.steps if s.position == t.step_position), None)


def alert_recipients(t: Ticket, step: TicketCategoryStep, db: Session) -> list:
    """Ids a avisar del SLA vencido en este paso (nunca el propio responsable)."""
    ids = []
    if step.alert_target == ALERT_BOSS:
        assignee = db.get(User, t.assigned_to) if t.assigned_to else None
        if assignee:
            ids = [b.id for b in direct_bosses(assignee, db)]
        if not ids:
            ids = department_admin_ids(t.department_id, db)
    elif step.alert_target == ALERT_DEPARTMENT and step.alert_department_id:
        ids = department_admin_ids(step.alert_department_id, db)
    return [i for i in dict.fromkeys(ids) if i != t.assigned_to]


def breached_metrics(t: Ticket, now: datetime) -> list[str]:
    status = sla_status(t, now)
    return [m for m in SLA_METRICS if status[m] and status[m]["state"] == "breached"]


def run_sla_alerts(now: datetime | None = None) -> int:
    """Manda los avisos pendientes; devuelve cuántos mandó (por persona)."""
    now = now or datetime.utcnow()
    sent = 0
    with SessionLocal() as db:
        tickets = (
            db.query(Ticket)
            .options(
                selectinload(Ticket.status_history),
                selectinload(Ticket.category).selectinload(TicketCategory.steps),
            )
            .filter(
                Ticket.category_id.isnot(None), Ticket.step_position.isnot(None),
                Ticket.status.in_([TICKET_OPEN, TICKET_IN_PROGRESS]),
                (Ticket.sla_response_minutes.isnot(None)) | (Ticket.sla_resolution_minutes.isnot(None)),
            )
            .all()
        )
        for t in tickets:
            step = current_step(t)
            if not step or not step.alert_target:
                continue
            metrics = breached_metrics(t, now)
            if not metrics:
                continue
            recipients = alert_recipients(t, step, db)
            if not recipients:
                continue
            for metric in metrics:
                rows = [
                    {"id": uuid.uuid4(), "ticket_id": t.id, "step_position": t.step_position,
                     "metric": metric, "user_id": uid, "created_at": now}
                    for uid in recipients
                ]
                claimed = db.execute(
                    pg_insert(TicketSlaAlert).values(rows)
                    .on_conflict_do_nothing(constraint="uq_ticket_sla_alerts")
                    .returning(TicketSlaAlert.user_id)
                ).scalars().all()
                db.commit()
                if claimed:
                    sent += len(claimed)
                    realtime.notify_users(
                        claimed, title=f"SLA vencido ({METRIC_LABELS[metric]}) en el paso {t.step_position}",
                        body=f"#{t.number} {t.title}", url=f"/tickets/{t.number}", kind="ticket_sla_alert",
                    )
    return sent


async def sla_alert_scheduler_loop() -> None:
    """Revisa cada minuto. Never raises out."""
    while True:
        try:
            n = await asyncio.to_thread(run_sla_alerts)
            if n:
                logger.info("ticket SLA alerts sent: %s", n)
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ticket SLA alert scheduler failed")
            await asyncio.sleep(POLL_SECONDS * 5)
