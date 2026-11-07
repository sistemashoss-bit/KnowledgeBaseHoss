"""Reglas de tickets: quién los ve y atiende, SLA y creación (la usan el router
y el widget de reportes).

Visibilidad: el solicitante, el asignado, todo el departamento receptor y quien
supervise ese departamento (su admin o un acceso extra). Superadmin y auditor
ven todos; el auditor solo participa en los de su propio departamento.
"""
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Department, Ticket, TicketEvidence, TicketSlaPolicy, TicketStatusHistory, User,
    PRIORITY_MEDIUM, ROLE_ADMIN, ROLE_AUDITOR, ROLE_SUPERADMIN,
    TASK_PRIORITIES, TICKET_CLOSED, TICKET_KINDS, TICKET_OPEN, TICKET_RESOLVED, TICKET_STATUSES, TICKET_WAITING,
)
from app.permissions import managed_department_ids, overseen_department_ids
from app.tasks.business_hours import business_seconds


# ── Permisos ──────────────────────────────────────────────────────────────────

def _same(a, b) -> bool:
    return a is not None and b is not None and str(a) == str(b)


def is_requester(user: User, t: Ticket) -> bool:
    return _same(t.requester_id, user.id)


def visible_condition(user: User):
    """Condición SQL de los tickets que ve, o None si ve todos."""
    if user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR):
        return None
    conds = [Ticket.requester_id == user.id, Ticket.assigned_to == user.id]
    depts = attended_department_ids(user) | overseen_department_ids(user)
    if depts:
        conds.append(Ticket.department_id.in_(depts))
    return or_(*conds)


def attended_department_ids(user: User) -> set:
    """Departamentos cuyos tickets atiende: el propio y los que gestiona."""
    ids = set(managed_department_ids(user))
    if user.department_id:
        ids.add(user.department_id)
    return ids


def can_view(user: User, t: Ticket) -> bool:
    if user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR):
        return True
    return (
        is_requester(user, t) or _same(t.assigned_to, user.id)
        or t.department_id in attended_department_ids(user) | overseen_department_ids(user)
    )


def can_attend(user: User, t: Ticket) -> bool:
    """Trabaja el ticket: cambia estado, prioridad y tipo, lo asigna dentro del
    departamento, lo transfiere, comenta y sube evidencias."""
    return (
        user.role == ROLE_SUPERADMIN or _same(t.assigned_to, user.id)
        or t.department_id in attended_department_ids(user)
    )


def can_manage(user: User, t: Ticket) -> bool:
    """Además de atender: edita título/descripción de cualquiera y borra
    comentarios y evidencias ajenos."""
    return user.role == ROLE_SUPERADMIN or t.department_id in managed_department_ids(user)


def can_comment(user: User, t: Ticket) -> bool:
    return can_attend(user, t) or is_requester(user, t)


def can_edit_text(user: User, t: Ticket) -> bool:
    """El solicitante corrige título y descripción mientras nadie lo ha respondido."""
    if can_manage(user, t):
        return True
    return is_requester(user, t) and t.first_response_at is None and t.status == TICKET_OPEN


def allowed_statuses(user: User, t: Ticket) -> list[str]:
    """Estados a los que puede moverlo (sin incluir el actual)."""
    if t.status == TICKET_CLOSED:
        return [TICKET_OPEN] if can_attend(user, t) else []
    if can_attend(user, t):
        return [s for s in TICKET_STATUSES if s != t.status]
    if is_requester(user, t):
        # Confirmar la solución o reabrirla; y retirarlo en cualquier momento.
        return [TICKET_OPEN, TICKET_CLOSED] if t.status == TICKET_RESOLVED else [TICKET_CLOSED]
    return []


def assignable_users(t: Ticket, db: Session) -> list[User]:
    """Personas activas del departamento que atiende el ticket."""
    return (
        db.query(User)
        .filter(User.department_id == t.department_id, User.is_active == True)  # noqa: E712
        .order_by(User.name, User.email).all()
    )


def department_admin_ids(department_id, db: Session, exclude=None) -> list:
    """Admins activos del departamento: reciben el aviso de un ticket sin asignar."""
    q = db.query(User.id).filter(
        User.department_id == department_id, User.role == ROLE_ADMIN, User.is_active == True,  # noqa: E712
    )
    if exclude:
        q = q.filter(User.id != exclude)
    return [uid for (uid,) in q]


def followers(t: Ticket, db: Session) -> set:
    """Quienes reciben avisos: solicitante y asignado; sin asignado, los admins del depto."""
    ids = {t.requester_id}
    if t.assigned_to:
        ids.add(t.assigned_to)
    else:
        ids.update(department_admin_ids(t.department_id, db))
    return ids


# ── SLA ───────────────────────────────────────────────────────────────────────

def policy_minutes(department_id, priority: str, db: Session) -> tuple[int | None, int | None]:
    """(respuesta, resolución) en minutos laborales: la del departamento o el default."""
    row = db.get(TicketSlaPolicy, (department_id, priority)) if department_id else None
    hours = settings.ticket_sla_hours.get(priority) or [None, None]
    defaults = [round(h * 60) if h else None for h in hours]
    # Lo que el departamento dejó vacío toma el default global.
    return (
        row.response_minutes if row and row.response_minutes is not None else defaults[0],
        row.resolution_minutes if row and row.resolution_minutes is not None else defaults[1],
    )


def apply_sla(t: Ticket, db: Session) -> None:
    t.sla_response_minutes, t.sla_resolution_minutes = policy_minutes(t.department_id, t.priority, db)


def _resolution_seconds(t: Ticket, now: datetime) -> float:
    """Horas laborales trabajadas: suma los tramos del historial salvo los de
    "En espera" (pausa) y los posteriores a resolverse o cerrarse."""
    history = t.status_history
    if not history:
        return business_seconds(t.created_at, t.resolved_at or now)
    total = 0.0
    for i, entry in enumerate(history):
        if entry.to_status in (TICKET_WAITING, TICKET_RESOLVED, TICKET_CLOSED):
            continue
        end = history[i + 1].changed_at if i + 1 < len(history) else now
        total += business_seconds(entry.changed_at, end)
    return total


def _metric(target_minutes: int | None, elapsed: float, done: bool) -> dict | None:
    if not target_minutes:
        return None
    target = target_minutes * 60
    ratio = elapsed / target if target else 0
    if done:
        state = "met" if elapsed <= target else "missed"
    elif elapsed > target:
        state = "breached"
    elif ratio >= settings.ticket_sla_warning_ratio:
        state = "risk"
    else:
        state = "ok"
    return {
        "state": state, "target": target, "elapsed": elapsed,
        "remaining": target - elapsed, "percent": min(100, round(ratio * 100)),
    }


def sla_status(t: Ticket, now: datetime | None = None) -> dict:
    """{"response": metric|None, "resolution": metric|None, "worst": state|None}.
    Estados: ok / risk (por vencer) / breached (vencido, sigue abierto) /
    met / missed (ya terminó, dentro o fuera de la meta)."""
    now = now or datetime.utcnow()
    response = _metric(
        t.sla_response_minutes,
        business_seconds(t.created_at, t.first_response_at or now),
        t.first_response_at is not None,
    )
    finished = t.status in (TICKET_RESOLVED, TICKET_CLOSED)
    resolution = _metric(t.sla_resolution_minutes, _resolution_seconds(t, now), finished)
    order = ["breached", "missed", "risk", "ok", "met"]
    states = [m["state"] for m in (response, resolution) if m]
    # Lo pendiente manda: una respuesta cumplida no oculta una resolución por vencer.
    pending = [s for s in states if s in ("breached", "risk", "ok")]
    pool = pending or states
    return {
        "response": response, "resolution": resolution,
        "worst": min(pool, key=order.index) if pool else None,
    }


# ── Cambios ───────────────────────────────────────────────────────────────────

def record_status(t: Ticket, new_status: str, actor: User, db: Session, when: datetime | None = None) -> None:
    """Cambia el estado y deja el registro; mantiene resolved_at/closed_at."""
    when = when or datetime.utcnow()
    old = t.status
    t.status = new_status
    if new_status == TICKET_RESOLVED:
        t.resolved_at = when
    elif new_status == TICKET_CLOSED:
        t.closed_at = when
        t.resolved_at = t.resolved_at or when
    else:
        t.resolved_at = None
        t.closed_at = None
    db.add(TicketStatusHistory(
        id=uuid.uuid4(), ticket_id=t.id, from_status=old, to_status=new_status,
        changed_by=actor.id, changed_at=when,
    ))


def mark_responded(t: Ticket, actor: User, when: datetime | None = None) -> None:
    """La primera acción visible de alguien que no es el solicitante cuenta como respuesta."""
    if t.first_response_at is None and not is_requester(actor, t):
        t.first_response_at = when or datetime.utcnow()


def create_ticket(
    db: Session, requester: User, *, kind: str, title: str, description: str,
    department_id, priority: str = PRIORITY_MEDIUM, source_app: str | None = None,
    context: dict | None = None,
) -> Ticket:
    """Crea el ticket (sin commit) con su registro de estado inicial y metas de SLA."""
    title = (title or "").strip()[:255]
    if not title:
        raise HTTPException(400, "El título es obligatorio")
    if kind not in TICKET_KINDS:
        raise HTTPException(400, "Tipo inválido")
    if priority not in TASK_PRIORITIES:
        raise HTTPException(400, "Prioridad inválida")
    try:
        dept = db.get(Department, uuid.UUID(str(department_id))) if department_id else None
    except ValueError:
        dept = None
    if not dept:
        raise HTTPException(400, "Elige el departamento que lo atiende")

    now = datetime.utcnow()
    t = Ticket(
        id=uuid.uuid4(), kind=kind, title=title, description=(description or "").strip() or None,
        status=TICKET_OPEN, priority=priority, department_id=dept.id, requester_id=requester.id,
        source_app=source_app, context=context, created_at=now, updated_at=now,
    )
    apply_sla(t, db)
    db.add(t)
    db.flush()
    db.add(TicketStatusHistory(
        id=uuid.uuid4(), ticket_id=t.id, from_status=None, to_status=TICKET_OPEN,
        changed_by=requester.id, changed_at=now,
    ))
    return t


def add_evidence(
    t: Ticket, user: User, content: bytes, filename: str, content_type: str, db: Session,
) -> TicketEvidence:
    """Sube el archivo al bucket de evidencias y lo registra (sin commit)."""
    import re
    from app import storage

    safe = re.sub(r"[^\w.\-]", "_", filename)[:200] or "file"
    key = f"tickets/{t.id}/{uuid.uuid4()}_{safe}"
    storage.upload_evidence(key, content, content_type)
    ev = TicketEvidence(
        id=uuid.uuid4(), ticket_id=t.id, uploaded_by=user.id, filename=filename,
        file_key=key, content_type=content_type, file_size=len(content),
    )
    db.add(ev)
    return ev
