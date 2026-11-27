"""Escalamiento automático de tickets con categoría (PLAN_CATEGORIAS_TICKETS.md).

Un ciclo de fondo cada minuto, lanzado desde el lifespan de main.py: los tickets
con categoría, abiertos o en atención ("En espera" pausa el SLA), cuyo SLA de
resolución del paso ya venció en horas laborales pasan al siguiente paso. En el
último paso no pasa nada (el SLA vencido solo se marca).

Cada ticket se bloquea con FOR UPDATE SKIP LOCKED antes de escalarlo, así que
varias instancias a la vez no lo escalan dos veces.
"""
import asyncio
import logging
from datetime import datetime

from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.messaging import realtime
from app.models import (
    ESCALATION_SLA, TICKET_IN_PROGRESS, TICKET_OPEN, Ticket, TicketCategory,
)
from app.tickets.categories import escalate, escalation_recipients
from app.tickets.service import sla_status

logger = logging.getLogger(__name__)

POLL_SECONDS = 60


def _candidates(db) -> list:
    return [
        row[0] for row in db.query(Ticket.id).filter(
            Ticket.category_id.isnot(None), Ticket.step_position.isnot(None),
            Ticket.sla_resolution_minutes.isnot(None),
            Ticket.status.in_([TICKET_OPEN, TICKET_IN_PROGRESS]),
        ).all()
    ]


def run_escalations(now: datetime | None = None) -> int:
    """Escala los tickets vencidos; devuelve cuántos escaló."""
    now = now or datetime.utcnow()
    done = 0
    with SessionLocal() as db:
        for ticket_id in _candidates(db):
            t = (
                db.query(Ticket)
                .options(
                    selectinload(Ticket.status_history),
                    selectinload(Ticket.category).selectinload(TicketCategory.steps),
                )
                .filter(Ticket.id == ticket_id)
                .with_for_update(skip_locked=True, of=Ticket)
                .first()
            )
            if not t or t.status not in (TICKET_OPEN, TICKET_IN_PROGRESS):
                db.rollback()
                continue
            resolution = sla_status(t, now)["resolution"]
            if not resolution or resolution["state"] != "breached":
                db.rollback()
                continue
            esc = escalate(t, db, reason=ESCALATION_SLA, when=now)
            if not esc:
                db.rollback()
                continue
            db.commit()
            done += 1
            label = f"#{t.number} {t.title}"
            realtime.notify_users(
                escalation_recipients(t, db), title="Un ticket se escaló por SLA vencido",
                body=label, url=f"/tickets/{t.number}", kind="ticket_escalate",
            )
            realtime.notify_ticket(t.id)
    return done


async def escalation_scheduler_loop() -> None:
    """Revisa cada minuto. Never raises out."""
    while True:
        try:
            n = await asyncio.to_thread(run_escalations)
            if n:
                logger.info("tickets escalated by SLA: %s", n)
            await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ticket escalation scheduler failed")
            await asyncio.sleep(POLL_SECONDS * 5)
