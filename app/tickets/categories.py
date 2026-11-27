"""Categorías de tickets y su cadena de pasos (PLAN_CATEGORIAS_TICKETS.md).

Solo las usa un departamento que tenga categorías activas (hoy Comercial): su gente
elige categoría en el widget en vez de departamento. Cada paso dice a quién le toca:
el jefe directo de quien reporta, un departamento o una persona. Un paso que no se
puede resolver (sin jefe, persona inactiva o sin departamento) se salta.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, selectinload

from app.models import (
    STEP_DEPARTMENT, STEP_DIRECT_BOSS, STEP_USER, TICKET_OPEN, Ticket, TicketCategory, TicketCategoryStep,
    TicketStatusHistory, User,
)
from app.tickets.hierarchy import pick_direct_boss

TARGET_LABELS = {STEP_DIRECT_BOSS: "Jefe directo", STEP_DEPARTMENT: "Departamento", STEP_USER: "Persona"}


@dataclass
class ResolvedStep:
    step: TicketCategoryStep
    department_id: object
    assigned_to: object | None


def active_categories(department_id, db: Session) -> list[TicketCategory]:
    """Categorías activas, con al menos un paso, del departamento de quien reporta."""
    if not department_id:
        return []
    rows = (
        db.query(TicketCategory)
        .options(selectinload(TicketCategory.steps))
        .filter(TicketCategory.department_id == department_id, TicketCategory.active == True)  # noqa: E712
        .order_by(TicketCategory.sort_order, TicketCategory.name)
        .all()
    )
    return [c for c in rows if c.steps]


ALERT_LABELS = {
    "boss": "Jefe del responsable",
    "department": "Departamento",
}


def alert_label(step: TicketCategoryStep) -> str:
    """A quién se avisa si se vence el SLA del paso; vacío = a nadie."""
    if step.alert_target == "department":
        return step.alert_department.name if step.alert_department else "Departamento"
    return ALERT_LABELS.get(step.alert_target or "", "")


def step_label(step: TicketCategoryStep) -> str:
    if step.target == STEP_DEPARTMENT and step.department:
        return step.department.name
    if step.target == STEP_USER and step.user:
        return step.user.name or step.user.email
    return TARGET_LABELS.get(step.target, step.target)


def resolve_step(category: TicketCategory, requester: User, db: Session, after: int = 0) -> ResolvedStep | None:
    """El primer paso con posición mayor a `after` que se pueda resolver; None si ya no hay."""
    for step in category.steps:
        if step.position <= after:
            continue
        if step.target == STEP_DIRECT_BOSS:
            boss = pick_direct_boss(requester, db)
            if boss and requester.department_id:
                return ResolvedStep(step, requester.department_id, boss.id)
        elif step.target == STEP_DEPARTMENT and step.department_id:
            return ResolvedStep(step, step.department_id, None)
        elif step.target == STEP_USER:
            person = db.get(User, step.user_id) if step.user_id else None
            if person and person.is_active and person.department_id:
                return ResolvedStep(step, person.department_id, person.id)
    return None


def apply_step(t: Ticket, category: TicketCategory, resolved: ResolvedStep, when: datetime | None = None) -> None:
    """Pone el ticket en ese paso: departamento, asignado y el SLA del paso, contado desde ahora."""
    t.category_id = category.id
    t.step_position = resolved.step.position
    t.step_started_at = when or datetime.utcnow()
    t.department_id = resolved.department_id
    t.assigned_to = resolved.assigned_to
    t.sla_response_minutes = resolved.step.sla_response_minutes
    t.sla_resolution_minutes = resolved.step.sla_resolution_minutes
    t.step_responded_at = None


def has_next_step(t: Ticket) -> bool:
    """Hay algún paso después del actual (puede que no se pueda resolver)."""
    if not t.category or t.step_position is None:
        return False
    return any(s.position > t.step_position for s in t.category.steps)


def escalate(
    t: Ticket, db: Session, *, actor: User | None = None, note: str | None = None, when: datetime | None = None,
) -> TicketStatusHistory | None:
    """Pasa el ticket al siguiente paso que se pueda resolver (sin commit) y lo deja
    en el historial de estados. None si ya no hay paso. Vuelve a "Abierto": el nuevo
    responsable no ha empezado. Quien lo tenía deja de verlo (regla de visibilidad)."""
    if not t.category or t.step_position is None:
        return None
    resolved = resolve_step(t.category, t.requester, db, after=t.step_position)
    if not resolved:
        return None
    when = when or datetime.utcnow()
    entry = TicketStatusHistory(
        id=uuid.uuid4(), ticket_id=t.id, from_status=t.status, to_status=TICKET_OPEN,
        changed_by=actor.id if actor else None, changed_at=when,
        from_position=t.step_position, to_position=resolved.step.position,
        to_department_id=resolved.department_id, to_user_id=resolved.assigned_to,
        note=(note or "").strip()[:2000] or None,
    )
    db.add(entry)
    apply_step(t, t.category, resolved, when=when)
    t.status = TICKET_OPEN
    t.resolved_at = None
    t.closed_at = None
    return entry


def escalation_recipients(t: Ticket, db: Session) -> list:
    """A quién avisar del nuevo paso: su responsable o, sin responsable, los admins
    del departamento; y al solicitante."""
    from app.tickets.service import department_admin_ids

    ids = [t.assigned_to] if t.assigned_to else department_admin_ids(t.department_id, db)
    return list({*ids, t.requester_id})
