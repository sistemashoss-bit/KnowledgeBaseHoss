from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.auth.deps import get_current_user
from app.database import get_db
from app.permissions import granted_department_ids, has_oversight
from app.models import (
    AuditLog, Department, Project, SearchLog,
    Task, TaskComment, TaskFollowUp, TaskShare, TaskStatusHistory, Ticket, TicketComment, User,
    ROLE_SUPERADMIN, ROLE_AUDITOR,
    TASK_STATUSES, TASK_PRIORITIES, TASK_DONE, TASK_PENDING, TASK_IN_PROGRESS, PROJECT_STATUSES,
    TICKET_ACTIVE_STATUSES, TICKET_CLOSED, TICKET_IN_PROGRESS, TICKET_OPEN, TICKET_RESOLVED, TICKET_WAITING,
)
from app.reports.export import open_tasks_pdf, open_tasks_xlsx, open_tickets_pdf, open_tickets_xlsx
from app.tickets import service as ticket_svc
from app.calendar.reminders import to_local
from app.tasks.business_hours import business_hours_label, business_seconds
from app.templating import templates

router = APIRouter(prefix="/reports", tags=["reports"])


# ── Scope helpers ─────────────────────────────────────────────────────────────

def _resolve_scope(current_user, department_id: str):
    """
    Returns (allowed_dept_ids, forced_dept_id).
    allowed_dept_ids=None means no restriction (superadmin/auditor, all).
    """
    if current_user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR):
        if department_id:
            return [department_id], department_id
        return None, ""

    # Su departamento más los de acceso extra (view o manage).
    ids = {str(d) for d in granted_department_ids(current_user)}
    own = str(current_user.department_id) if current_user.department_id else ""
    if own:
        ids.add(own)
    if department_id and department_id in ids:
        return [department_id], department_id
    return sorted(ids), own if len(ids) == 1 else ""


def _resolve_filters(current_user, department_id: str, user_id: str, db: Session):
    """(dept_ids, active_dept, dept_users, active_user). Filtro de usuario
    (asignado): solo con un departamento elegido y entre los usuarios activos
    de ese departamento."""
    dept_ids, active_dept = _resolve_scope(current_user, department_id)
    dept_users = (
        db.query(User)
        .filter(User.department_id == active_dept, User.is_active.is_(True))
        .order_by(User.name, User.email)
        .all()
        if active_dept else []
    )
    active_user = user_id if any(str(u.id) == user_id for u in dept_users) else ""
    return dept_ids, active_dept, dept_users, active_user


# ── Data helpers ──────────────────────────────────────────────────────────────

def _task_stats(dept_ids, dt_from: datetime, dt_to: datetime, db: Session, user_id: str = "") -> dict:
    q = db.query(Task).filter(Task.created_at >= dt_from, Task.created_at < dt_to)
    if dept_ids is not None:
        q = q.filter(Task.department_id.in_(dept_ids)) if dept_ids else q.filter(Task.id == None)
    if user_id:
        q = q.filter(Task.assigned_to == user_id)
    tasks = q.all()

    today_str = date.today().isoformat()
    by_status = defaultdict(int)
    by_priority = defaultdict(int)
    for t in tasks:
        by_status[t.status] += 1
        by_priority[t.priority] += 1

    overdue = db.query(func.count(Task.id)).filter(
        Task.due_date < date.today(),
        Task.status != "done",
    )
    if dept_ids is not None:
        overdue = overdue.filter(Task.department_id.in_(dept_ids)) if dept_ids else overdue.filter(Task.id == None)
    if user_id:
        overdue = overdue.filter(Task.assigned_to == user_id)
    overdue_count = overdue.scalar() or 0

    return {
        "total": len(tasks),
        "done": by_status["done"],
        "in_progress": by_status["in_progress"],
        "pending": by_status["pending"],
        "review": by_status["review"],
        "overdue": overdue_count,
        "by_status": dict(by_status),
        "by_priority": dict(by_priority),
    }


def _tasks_over_time(dept_ids, dt_from: datetime, dt_to: datetime, db: Session, user_id: str = "") -> list[dict]:
    """Returns list of {date, created, completed} for each day in range."""
    q = db.query(Task).filter(Task.created_at >= dt_from, Task.created_at < dt_to)
    if dept_ids is not None:
        q = q.filter(Task.department_id.in_(dept_ids)) if dept_ids else q.filter(Task.id == None)
    if user_id:
        q = q.filter(Task.assigned_to == user_id)
    tasks = q.all()

    created_by_day: dict[str, int] = defaultdict(int)
    done_by_day: dict[str, int] = defaultdict(int)
    for t in tasks:
        created_by_day[t.created_at.date().isoformat()] += 1
        if t.status == "done" and t.updated_at:
            done_by_day[t.updated_at.date().isoformat()] += 1

    days = []
    cur = dt_from.date()
    end = dt_to.date()
    while cur < end:
        s = cur.isoformat()
        days.append({"date": s, "created": created_by_day[s], "completed": done_by_day[s]})
        cur += timedelta(days=1)
    return days


def _dept_time_stats(dept_ids, dt_from: datetime, dt_to: datetime, db: Session, user_id: str = "") -> list[dict]:
    """Horas acumuladas por departamento en el período: para cada tarea que llegó
    a 'done' (mismo universo que _task_stats, filtrado por fecha de creación),
    suma sus horas laborales del primer estado al paso a "Listo", según el
    historial — el mismo "Total" del detalle de la tarea. Sin historial (tareas
    anteriores a él) se usa creación → `updated_at`, también en horas laborales."""
    q = db.query(Task).filter(
        Task.created_at >= dt_from, Task.created_at < dt_to, Task.status == TASK_DONE,
    )
    if dept_ids is not None:
        q = q.filter(Task.department_id.in_(dept_ids)) if dept_ids else q.filter(Task.id == None)
    if user_id:
        q = q.filter(Task.assigned_to == user_id)
    tasks = q.all()

    # Inicio y fin por tarea en UNA consulta (no el historial completo de cada una).
    spans = {
        r.task_id: (r.first_at, r.last_at)
        for r in db.query(
            TaskStatusHistory.task_id,
            func.min(TaskStatusHistory.changed_at).label("first_at"),
            func.max(TaskStatusHistory.changed_at).label("last_at"),
        ).filter(TaskStatusHistory.task_id.in_([t.id for t in tasks])).group_by(TaskStatusHistory.task_id)
    } if tasks else {}

    seconds_by_dept: dict[str, float] = defaultdict(float)
    count_by_dept: dict[str, int] = defaultdict(int)
    for t in tasks:
        start, end = spans.get(t.id) or (t.created_at, t.updated_at)
        if not start or not end:
            continue
        elapsed = business_seconds(start, end)
        key = str(t.department_id) if t.department_id else ""
        seconds_by_dept[key] += elapsed
        count_by_dept[key] += 1

    names = {
        str(d.id): d.name
        for d in db.query(Department).filter(Department.id.in_([k for k in seconds_by_dept if k])).all()
    }

    result = []
    for key, secs in seconds_by_dept.items():
        hours = secs / 3600
        cnt = count_by_dept[key]
        result.append({
            "department": names.get(key, "Sin departamento"),
            "tasks": cnt,
            "hours": round(hours, 1),
            "avg_hours": round(hours / cnt, 1) if cnt else 0,
        })
    result.sort(key=lambda d: d["hours"], reverse=True)
    return result


def _dept_ticket_time_stats(dept_ids, dt_from: datetime, dt_to: datetime, db: Session, user_id: str = "") -> list[dict]:
    """Lo mismo que _dept_time_stats pero de tickets: los levantados en el período
    que ya están resueltos o cerrados, por departamento que los atendió. Las horas
    son laborales y trabajadas (sin los tramos "En espera"), las mismas del SLA de
    resolución. `user_id`: el responsable asignado."""
    q = db.query(Ticket).options(selectinload(Ticket.status_history)).filter(
        Ticket.created_at >= dt_from, Ticket.created_at < dt_to,
        Ticket.status.in_([TICKET_RESOLVED, TICKET_CLOSED]),
    )
    if dept_ids is not None:
        q = q.filter(Ticket.department_id.in_(dept_ids)) if dept_ids else q.filter(Ticket.id == None)  # noqa: E711
    if user_id:
        q = q.filter(Ticket.assigned_to == user_id)

    agg: dict[str, dict] = defaultdict(lambda: {"seconds": 0.0, "tickets": 0, "sla_total": 0, "sla_met": 0})
    for t in q.all():
        a = agg[str(t.department_id)]
        a["seconds"] += ticket_svc.resolution_seconds(t, t.resolved_at or t.closed_at)
        a["tickets"] += 1
        resolution = ticket_svc.sla_status(t)["resolution"]
        if resolution:
            a["sla_total"] += 1
            a["sla_met"] += resolution["state"] == "met"

    names = {str(d.id): d.name for d in db.query(Department).filter(Department.id.in_(list(agg))).all()} if agg else {}
    result = []
    for key, a in agg.items():
        hours = a["seconds"] / 3600
        result.append({
            "department": names.get(key, "Sin departamento"),
            "tickets": a["tickets"],
            "hours": round(hours, 1),
            "avg_hours": round(hours / a["tickets"], 1) if a["tickets"] else 0,
            "sla_total": a["sla_total"],
            "sla_met": a["sla_met"],
            "sla_percent": round(a["sla_met"] * 100 / a["sla_total"]) if a["sla_total"] else None,
        })
    result.sort(key=lambda d: d["hours"], reverse=True)
    return result


FOLLOW_UPS_SHOWN = 6


def _follow_up_window(items: list[dict], limit: int = FOLLOW_UPS_SHOWN) -> list[dict]:
    """Los seguimientos más cercanos a hoy (una serie diaria puede tener 60): los
    últimos ya ocurridos y los próximos, en orden de fecha."""
    if len(items) <= limit:
        return items
    first_upcoming = next((i for i, f in enumerate(items) if f["state"] == "upcoming"), len(items))
    start = max(0, min(first_upcoming - limit // 2, len(items) - limit))
    return items[start:start + limit]


def _open_tasks(conds: list, db: Session) -> list[dict]:
    """Tareas pendientes y en progreso (no archivadas) que cumplen `conds`,
    agrupadas por departamento, con sus seguimientos y su último comentario. Es
    el estado actual: no se acota por un rango de fechas."""
    q = (
        db.query(Task)
        .options(joinedload(Task.assignee), joinedload(Task.department))
        .filter(Task.status.in_([TASK_PENDING, TASK_IN_PROGRESS]), Task.archived_at.is_(None), *conds)
    )
    tasks = q.order_by(Task.due_date.asc().nullslast(), Task.created_at).all()
    if not tasks:
        return []
    task_ids = [t.id for t in tasks]

    # Cada seguimiento con su fecha (hora local) y estado: hecho, no hecho (ya
    # pasó su hora sin marcarse) o próximo.
    now = datetime.utcnow()
    follow_ups: dict = defaultdict(list)
    for f in (
        db.query(TaskFollowUp.task_id, TaskFollowUp.start_at, TaskFollowUp.end_at, TaskFollowUp.done_at)
        .filter(TaskFollowUp.task_id.in_(task_ids))
        .order_by(TaskFollowUp.start_at)
    ):
        follow_ups[f.task_id].append({
            "date": to_local(f.start_at).date(),
            "state": "done" if f.done_at else ("missed" if f.end_at < now else "upcoming"),
        })

    # Último comentario por tarea (DISTINCT ON de Postgres).
    last_comments = {
        c.task_id: c
        for c in db.query(TaskComment)
        .options(joinedload(TaskComment.user))
        .filter(TaskComment.task_id.in_(task_ids))
        .order_by(TaskComment.task_id, TaskComment.created_at.desc())
        .distinct(TaskComment.task_id)
    }

    groups: dict[str, dict] = {}
    for t in tasks:
        key = str(t.department_id) if t.department_id else ""
        g = groups.setdefault(key, {
            "department": t.department.name if t.department else "Sin departamento",
            "tasks": [], "pending": 0, "in_progress": 0,
        })
        g["pending" if t.status == TASK_PENDING else "in_progress"] += 1
        fus = follow_ups.get(t.id, [])
        shown = _follow_up_window(fus)
        c = last_comments.get(t.id)
        g["tasks"].append({
            "id": t.id,
            "title": t.title,
            "status": t.status,
            "assignee": (t.assignee.name or t.assignee.email) if t.assignee else "",
            "due_date": t.due_date,
            "overdue": bool(t.due_date and t.due_date < date.today()),
            "follow_ups": fus,
            "follow_ups_shown": shown,
            "follow_ups_hidden": len(fus) - len(shown),
            "last_comment": c.content if c else "",
            "last_comment_by": ((c.user.name or c.user.email) if c.user else "") if c else "",
            "last_comment_at": c.created_at if c else None,
        })
    return sorted(groups.values(), key=lambda g: g["department"].lower())


def _project_stats(dept_ids, dt_from: datetime, dt_to: datetime, db: Session) -> dict:
    q = db.query(Project).filter(Project.created_at >= dt_from, Project.created_at < dt_to)
    if dept_ids is not None:
        q = q.filter(Project.department_id.in_(dept_ids)) if dept_ids else q.filter(Project.id == None)
    projects = q.all()

    by_status = defaultdict(int)
    for p in projects:
        by_status[p.status] += 1

    # active total (not date-filtered)
    active_q = db.query(func.count(Project.id)).filter(Project.status == "active")
    if dept_ids is not None:
        active_q = active_q.filter(Project.department_id.in_(dept_ids)) if dept_ids else active_q.filter(Project.id == None)
    active_total = active_q.scalar() or 0

    return {
        "total": len(projects),
        "active_total": active_total,
        "by_status": dict(by_status),
    }


def _top_users(dt_from: datetime, dt_to: datetime, db: Session, limit: int = 10) -> list:
    rows = (
        db.query(AuditLog.user_email, func.count(AuditLog.id).label("actions"),
                 func.max(AuditLog.created_at).label("last_at"))
        .filter(AuditLog.created_at >= dt_from, AuditLog.created_at < dt_to)
        .filter(AuditLog.user_email.isnot(None))
        .group_by(AuditLog.user_email)
        .order_by(func.count(AuditLog.id).desc())
        .limit(limit)
        .all()
    )
    return [{"email": r.user_email, "actions": r.actions, "last_at": r.last_at} for r in rows]


def _top_searches(dt_from: datetime, dt_to: datetime, db: Session, limit: int = 10) -> list:
    rows = (
        db.query(SearchLog.query, func.count(SearchLog.id).label("count"),
                 func.avg(SearchLog.result_count).label("avg_results"))
        .filter(SearchLog.created_at >= dt_from, SearchLog.created_at < dt_to)
        .group_by(SearchLog.query)
        .order_by(func.count(SearchLog.id).desc())
        .limit(limit)
        .all()
    )
    return [{"query": r.query, "count": r.count, "avg_results": round(r.avg_results or 0, 1)} for r in rows]


def _department_options(current_user, db: Session) -> list:
    """Departamentos para el filtro: todos para superadmin/auditor; para el resto,
    los suyos (propio + acceso extra) solo si son más de uno."""
    if current_user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR):
        return db.query(Department).order_by(Department.name).all()
    own_dept_ids = [current_user.department_id] if current_user.department_id else []
    scoped = list(granted_department_ids(current_user)) + own_dept_ids
    return (
        db.query(Department).filter(Department.id.in_(scoped)).order_by(Department.name).all()
        if len(set(scoped)) > 1 else []
    )


def _require_reports_access(current_user) -> None:
    if not current_user.department_id and not has_oversight(current_user) and current_user.role != ROLE_AUDITOR:
        raise HTTPException(403)


# ── Resumen de Tareas ─────────────────────────────────────────────────────────

# Relación de la tarea con el usuario elegido en el filtro (como las pestañas
# de Tareas: asignadas a, creadas por, compartidas con).
TASK_USER_RELATIONS = [
    ("all", "Todos"),
    ("assigned", "Asignadas al usuario"),
    ("created", "Asignadas por el usuario"),
    ("shared", "Compartidas con el usuario"),
]


def _task_report_scope(current_user, department_id: str, user_id: str, relation: str, db: Session) -> dict:
    """Condiciones de la consulta y estado de los filtros del Resumen de Tareas."""
    if relation not in dict(TASK_USER_RELATIONS):
        relation = "all"
    dept_ids, active_dept, dept_users, active_user = _resolve_filters(current_user, department_id, user_id, db)
    conds = []
    if dept_ids is not None:
        conds.append(Task.department_id.in_(dept_ids) if dept_ids else Task.id == None)  # noqa: E711
    if active_user:
        by_relation = {
            "assigned": Task.assigned_to == active_user,
            "created": Task.created_by == active_user,
            "shared": Task.shares.any(TaskShare.user_id == active_user),
        }
        conds.append(or_(*by_relation.values()) if relation == "all" else by_relation[relation])
    departments = _department_options(current_user, db)

    subtitle = []
    dept = next((d for d in departments if str(d.id) == active_dept), None) or (
        db.get(Department, active_dept) if active_dept else None
    )
    if dept:
        subtitle.append(f"Departamento: {dept.name}")
    for u in dept_users:
        if str(u.id) == active_user:
            subtitle.append(f"Usuario: {u.name or u.email}")
            if relation != "all":
                subtitle.append(dict(TASK_USER_RELATIONS)[relation])
    return {
        "conds": conds, "departments": departments,
        "active_dept": active_dept, "dept_users": dept_users, "active_user": active_user,
        "relation": relation, "subtitle": subtitle,
    }


@router.get("/tasks/", response_class=HTMLResponse)
def tasks_report(
    request: Request,
    department_id: str = "",
    user_id: str = "",
    relation: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    _require_reports_access(current_user)

    scope = _task_report_scope(current_user, department_id, user_id, relation, db)
    groups = _open_tasks(scope["conds"], db)
    all_tasks = [t for g in groups for t in g["tasks"]]
    return templates.TemplateResponse(
        request,
        "reports/tasks.html",
        {
            "current_user": current_user,
            "relations": TASK_USER_RELATIONS,
            "relation": scope["relation"],
            "departments": scope["departments"],
            "active_dept": scope["active_dept"],
            "dept_users": scope["dept_users"],
            "active_user": scope["active_user"],
            "subtitle": " · ".join(scope["subtitle"]) or "Todos los departamentos",
            "open_tasks": groups,
            "totals": {
                "pending": sum(g["pending"] for g in groups),
                "in_progress": sum(g["in_progress"] for g in groups),
                "overdue": sum(1 for t in all_tasks if t["overdue"]),
            },
        },
    )


@router.get("/tasks/export.{fmt}")
def export_tasks_report(
    fmt: str,
    department_id: str = "",
    user_id: str = "",
    relation: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Exporta el Resumen de Tareas con los mismos filtros de la página."""
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    _require_reports_access(current_user)
    if fmt not in ("xlsx", "pdf"):
        raise HTTPException(404)

    scope = _task_report_scope(current_user, department_id, user_id, relation, db)
    groups = _open_tasks(scope["conds"], db)
    generated = datetime.now()
    subtitle = " · ".join(scope["subtitle"] + [f"Generado: {generated.strftime('%d/%m/%Y %H:%M')}"])

    filename = f"resumen-tareas-{generated.strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "xlsx":
        content = open_tasks_xlsx(groups, subtitle)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = open_tasks_pdf(groups, subtitle)
        media = "application/pdf"
    return Response(
        content, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Resumen de Tickets ────────────────────────────────────────────────────────

TICKET_USER_RELATIONS = [
    ("all", "Todos"),
    ("assigned", "Asignados al usuario"),
    ("requested", "Levantados por el usuario"),
]
_TICKET_PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
_SLA_ORDER = {"breached": 0, "risk": 1, "ok": 2}


def _open_tickets(conds: list, db: Session) -> list[dict]:
    """Tickets activos (abiertos, en atención, en espera) que cumplen `conds`,
    agrupados por el departamento que los atiende, con su SLA y su último
    comentario. Es el estado actual: no se acota por fechas. Primero los de SLA
    vencido, luego por vencer; dentro, por prioridad y antigüedad."""
    tickets = (
        db.query(Ticket)
        .options(
            joinedload(Ticket.assignee), joinedload(Ticket.requester), joinedload(Ticket.department),
            selectinload(Ticket.status_history),
        )
        .filter(Ticket.status.in_(TICKET_ACTIVE_STATUSES), *conds)
        .all()
    )
    if not tickets:
        return []
    last_comments = {
        c.ticket_id: c
        for c in db.query(TicketComment)
        .options(joinedload(TicketComment.user))
        .filter(TicketComment.ticket_id.in_([t.id for t in tickets]))
        .order_by(TicketComment.ticket_id, TicketComment.created_at.desc())
        .distinct(TicketComment.ticket_id)
    }
    now = datetime.utcnow()
    groups: dict[str, dict] = {}
    for t in tickets:
        g = groups.setdefault(str(t.department_id), {
            "department": t.department.name if t.department else "Sin departamento",
            "tickets": [], TICKET_OPEN: 0, TICKET_IN_PROGRESS: 0, TICKET_WAITING: 0, "breached": 0,
        })
        g[t.status] += 1
        sla = ticket_svc.sla_status(t, now)
        g["breached"] += sla["worst"] == "breached"
        # La meta que sigue corriendo: la respuesta mientras no haya, si no la resolución.
        pending = next((m for m in (sla["response"], sla["resolution"]) if m and m["state"] in ("ok", "risk", "breached")), None)
        c = last_comments.get(t.id)
        g["tickets"].append({
            "number": t.number,
            "title": t.title,
            "kind": t.kind,
            "priority": t.priority,
            "status": t.status,
            "assignee": (t.assignee.name or t.assignee.email) if t.assignee else "",
            "requester": (t.requester.name or t.requester.email) if t.requester else "",
            "created_at": t.created_at,
            "sla_state": sla["worst"],
            "sla_metric": pending,
            "sla_metric_name": (
                "Primera respuesta" if pending is not None and pending is sla["response"] else "Resolución"
            ) if pending else "",
            "last_comment": c.content if c else "",
            "last_comment_by": ((c.user.name or c.user.email) if c.user else "") if c else "",
            "last_comment_at": c.created_at if c else None,
        })
    for g in groups.values():
        g["tickets"].sort(key=lambda r: (
            _SLA_ORDER.get(r["sla_state"], 3), _TICKET_PRIORITY_ORDER.get(r["priority"], 9), r["created_at"],
        ))
    return sorted(groups.values(), key=lambda g: g["department"].lower())


def _ticket_report_scope(current_user, department_id: str, user_id: str, relation: str, db: Session) -> dict:
    """Condiciones y estado de los filtros del Resumen de Tickets (como el de Tareas)."""
    if relation not in dict(TICKET_USER_RELATIONS):
        relation = "all"
    dept_ids, active_dept, dept_users, active_user = _resolve_filters(current_user, department_id, user_id, db)
    conds = []
    if dept_ids is not None:
        conds.append(Ticket.department_id.in_(dept_ids) if dept_ids else Ticket.id == None)  # noqa: E711
    if active_user:
        by_relation = {"assigned": Ticket.assigned_to == active_user, "requested": Ticket.requester_id == active_user}
        conds.append(or_(*by_relation.values()) if relation == "all" else by_relation[relation])
    departments = _department_options(current_user, db)

    subtitle = []
    dept = next((d for d in departments if str(d.id) == active_dept), None) or (
        db.get(Department, active_dept) if active_dept else None
    )
    if dept:
        subtitle.append(f"Departamento: {dept.name}")
    for u in dept_users:
        if str(u.id) == active_user:
            subtitle.append(f"Usuario: {u.name or u.email}")
            if relation != "all":
                subtitle.append(dict(TICKET_USER_RELATIONS)[relation])
    return {
        "conds": conds, "departments": departments,
        "active_dept": active_dept, "dept_users": dept_users, "active_user": active_user,
        "relation": relation, "subtitle": subtitle,
    }


@router.get("/tickets/", response_class=HTMLResponse)
def tickets_report(
    request: Request,
    department_id: str = "",
    user_id: str = "",
    relation: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    _require_reports_access(current_user)

    scope = _ticket_report_scope(current_user, department_id, user_id, relation, db)
    groups = _open_tickets(scope["conds"], db)
    return templates.TemplateResponse(
        request,
        "reports/tickets.html",
        {
            "current_user": current_user,
            "relations": TICKET_USER_RELATIONS,
            "relation": scope["relation"],
            "departments": scope["departments"],
            "active_dept": scope["active_dept"],
            "dept_users": scope["dept_users"],
            "active_user": scope["active_user"],
            "subtitle": " · ".join(scope["subtitle"]) or "Todos los departamentos",
            "open_tickets": groups,
            "business_hours_label": business_hours_label(),
            "totals": {
                "open": sum(g[TICKET_OPEN] for g in groups),
                "in_progress": sum(g[TICKET_IN_PROGRESS] for g in groups),
                "waiting": sum(g[TICKET_WAITING] for g in groups),
                "breached": sum(g["breached"] for g in groups),
            },
        },
    )


@router.get("/tickets/export.{fmt}")
def export_tickets_report(
    fmt: str,
    department_id: str = "",
    user_id: str = "",
    relation: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Exporta el Resumen de Tickets con los mismos filtros de la página."""
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    _require_reports_access(current_user)
    if fmt not in ("xlsx", "pdf"):
        raise HTTPException(404)

    scope = _ticket_report_scope(current_user, department_id, user_id, relation, db)
    groups = _open_tickets(scope["conds"], db)
    generated = datetime.now()
    subtitle = " · ".join(scope["subtitle"] + [f"Generado: {generated.strftime('%d/%m/%Y %H:%M')}"])

    filename = f"resumen-tickets-{generated.strftime('%Y%m%d-%H%M')}.{fmt}"
    if fmt == "xlsx":
        content = open_tickets_xlsx(groups, subtitle)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = open_tickets_pdf(groups, subtitle)
        media = "application/pdf"
    return Response(
        content, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Resumen General ───────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def reports_dashboard(
    request: Request,
    department_id: str = "",
    user_id: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    _require_reports_access(current_user)

    # Date range defaults: last 30 days
    try:
        dt_from = datetime.fromisoformat(date_from) if date_from else datetime.utcnow() - timedelta(days=30)
    except ValueError:
        dt_from = datetime.utcnow() - timedelta(days=30)
    try:
        dt_to = datetime.fromisoformat(date_to) + timedelta(days=1) if date_to else datetime.utcnow() + timedelta(days=1)
    except ValueError:
        dt_to = datetime.utcnow() + timedelta(days=1)

    dept_ids, active_dept, dept_users, active_user = _resolve_filters(current_user, department_id, user_id, db)

    # Aggregate
    task_stats = _task_stats(dept_ids, dt_from, dt_to, db, active_user)
    dept_time_stats = _dept_time_stats(dept_ids, dt_from, dt_to, db, active_user)
    dept_ticket_stats = _dept_ticket_time_stats(dept_ids, dt_from, dt_to, db, active_user)
    tasks_timeline = _tasks_over_time(dept_ids, dt_from, dt_to, db, active_user)

    # Proyectos: superadmin y auditor. Usuarios más activos y búsquedas
    # frecuentes: solo superadmin (salen del audit log, son globales sin acotar
    # por depto — actividad de uso de la plataforma, no de los departamentos).
    is_org_wide = current_user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR)
    if is_org_wide:
        project_stats = _project_stats(dept_ids, dt_from, dt_to, db)
    else:
        project_stats = {"total": 0, "active_total": 0, "by_status": {}}
    if current_user.role == ROLE_SUPERADMIN:
        top_users = _top_users(dt_from, dt_to, db)
        top_searches = _top_searches(dt_from, dt_to, db)
    else:
        top_users = []
        top_searches = []

    # Chart.js data
    timeline_labels = [d["date"] for d in tasks_timeline]
    timeline_created = [d["created"] for d in tasks_timeline]
    timeline_completed = [d["completed"] for d in tasks_timeline]

    status_labels = ["Pendiente", "En progreso", "Revisión", "Listo"]
    status_keys = ["pending", "in_progress", "review", "done"]
    status_data = [task_stats["by_status"].get(k, 0) for k in status_keys]

    priority_labels = ["Baja", "Media", "Alta", "Urgente"]
    priority_keys = ["low", "medium", "high", "urgent"]
    priority_data = [task_stats["by_priority"].get(k, 0) for k in priority_keys]

    departments = _department_options(current_user, db)

    return templates.TemplateResponse(
        request,
        "reports/dashboard.html",
        {
            "current_user": current_user,
            "is_org_wide": is_org_wide,
            # filters
            "departments": departments,
            "active_dept": active_dept,
            "dept_users": dept_users,
            "active_user": active_user,
            "date_from": date_from or (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d"),
            "date_to": date_to or date.today().isoformat(),
            # KPIs
            "task_stats": task_stats,
            "dept_time_stats": dept_time_stats,
            "dept_ticket_stats": dept_ticket_stats,
            "business_hours_label": business_hours_label(),
            "project_stats": project_stats,
            # tables
            "top_users": top_users,
            "top_searches": top_searches,
            # chart data (JSON-safe)
            "timeline_labels": timeline_labels,
            "timeline_created": timeline_created,
            "timeline_completed": timeline_completed,
            "status_labels": status_labels,
            "status_data": status_data,
            "priority_labels": priority_labels,
            "priority_data": priority_data,
        },
    )
