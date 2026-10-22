"""Calendario personal: citas y actividades de cada usuario, más los calendarios
ajenos que tiene agregados (admin → su gente; superadmin → cualquiera) y,
opcionalmente, las fechas límite de tareas y proyectos.

Los seguimientos de tareas (TaskFollowUp) salen siempre, como bloques con hora,
en el calendario de su responsable (el asignado, o el creador si no hay).

Los eventos pueden repetirse con los mismos criterios que las tareas
recurrentes (diario, semanal, mensual, días específicos); las ocurrencias se
calculan al consultar y la serie se edita completa. Los eventos aceptan
comentarios del dueño y de quien puede ver su calendario.

Fechas: se guardan en UTC; hacia y desde el navegador viajan en hora local del
negocio sin offset (FullCalendar corre con timeZone 'UTC' para mostrarlas tal
cual, igual que el resto de la app muestra todo fijo en hora de Monterrey).
"""
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from app import audit
from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.calendar.reminders import STALE_AFTER, iter_occurrences, next_remind_at, to_local, to_utc
from app.database import get_db
from app.models import (
    CalendarEvent, CalendarEventComment, CalendarSubscription, Department, Project, Task, TaskFollowUp,
    TaskTagAssignment, User,
    CALENDAR_MAX_SUBSCRIPTIONS, CALENDAR_REMINDER_OPTIONS, PROJECT_COMPLETED,
    ROLE_SUPERADMIN, TASK_DONE, TASK_TAG_COLORS,
)
from app.messaging import realtime
from app.permissions import (
    calendar_department_ids, calendar_members_query, granted_department_ids, has_oversight, is_manager,
    manager_scope, visible_projects_filter,
)
from app.tasks.recurring import parse_recurrence
from app.templating import templates

router = APIRouter(prefix="/calendar", tags=["calendar"])

_PRIORITY_COLORS = {"low": "gray", "medium": "blue", "high": "orange", "urgent": "red"}
_TASK_STATUS_LABELS = {
    "pending": "Pendiente", "in_progress": "En progreso", "review": "En revisión", "done": "Listo",
}
_PRIORITY_LABELS = {"low": "Baja", "medium": "Media", "high": "Alta", "urgent": "Urgente"}
_PROJECT_STATUS_LABELS = {"draft": "Borrador", "active": "Activo", "on_hold": "En pausa", "completed": "Completado"}
_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_WEEKDAYS_SHORT = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _can_subscribe(user: User) -> bool:
    return has_oversight(user)


def _display_name(user: User | None) -> str:
    if not user:
        return ""
    return user.name or user.email


def _initials(user: User | None) -> str:
    parts = _display_name(user).replace("@", " ").split()
    return "".join(p[0] for p in parts[:2]).upper()


def _parse_local(value: str) -> datetime:
    """'YYYY-MM-DD' o 'YYYY-MM-DDTHH:MM[:SS]' en hora local (sin offset; si
    trae uno —FullCalendar manda 'Z' con timeZone UTC— se descarta, porque la
    hora de pared ya es la local)."""
    value = (value or "").strip()
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), datetime.min.time())
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None, second=0, microsecond=0)
    except ValueError:
        raise HTTPException(422, "Fecha inválida")


def _iso_local(dt_utc: datetime, all_day: bool) -> str:
    local = to_local(dt_utc)
    return local.date().isoformat() if all_day else local.isoformat(timespec="minutes")


def _parse_reminder(value: str) -> int | None:
    if value in ("", None):
        return None
    try:
        minutes = int(value)
    except ValueError:
        raise HTTPException(422, "Recordatorio inválido")
    if minutes not in CALENDAR_REMINDER_OPTIONS:
        raise HTTPException(422, "Recordatorio inválido")
    return minutes


def _apply_schedule(ev: CalendarEvent, start: str, end: str, all_day: bool) -> None:
    """Fija fechas (entrada en hora local; en todo el día `end` es exclusivo) y
    recalcula el recordatorio pendiente."""
    start_local = _parse_local(start)
    end_local = _parse_local(end) if end else None
    if all_day:
        start_local = datetime.combine(start_local.date(), datetime.min.time())
        if end_local:
            end_local = datetime.combine(end_local.date(), datetime.min.time())
        if not end_local or end_local <= start_local:
            end_local = start_local + timedelta(days=1)
    elif not end_local or end_local <= start_local:
        end_local = start_local + timedelta(hours=1)

    new_start, new_end = to_utc(start_local), to_utc(end_local)
    changed = (ev.start_at, ev.all_day) != (new_start, all_day)
    ev.start_at, ev.end_at, ev.all_day = new_start, new_end, all_day
    if changed or ev.remind_at is None:
        _refresh_reminder(ev)


def _refresh_reminder(ev: CalendarEvent) -> None:
    # Desde STALE_AFTER atrás, igual que un evento sin repetición recién creado
    # cuyo aviso ya pasó hace poco: todavía se envía.
    ev.remind_at = next_remind_at(ev, datetime.utcnow() - STALE_AFTER)
    ev.reminded_at = None


def _apply_recurrence(
    ev: CalendarEvent, repeat: bool, frequency: str, day_of_week: str, day_of_month: str,
    days_of_week: list[str], repeat_until: str,
) -> bool:
    """Fija la repetición (mismas reglas que las tareas recurrentes). Llamar
    después de `_apply_schedule`. Devuelve True si cambió."""
    before = (ev.frequency, ev.day_of_week, ev.day_of_month, ev.days_of_week, ev.repeat_until)
    if repeat:
        freq, dow, dom, custom = parse_recurrence(frequency, day_of_week, day_of_month, days_of_week)
        try:
            until = date.fromisoformat(repeat_until) if repeat_until else None
        except ValueError:
            raise HTTPException(422, "Fecha de fin de repetición inválida")
        if until and until < to_local(ev.start_at).date():
            raise HTTPException(422, "La repetición debe terminar después del inicio")
        ev.frequency, ev.day_of_week, ev.day_of_month, ev.days_of_week, ev.repeat_until = freq, dow, dom, custom, until
    else:
        ev.frequency = ev.day_of_week = ev.day_of_month = ev.days_of_week = ev.repeat_until = None
    return before != (ev.frequency, ev.day_of_week, ev.day_of_month, ev.days_of_week, ev.repeat_until)


def _repeat_label(ev: CalendarEvent) -> str:
    if not ev.frequency:
        return ""
    if ev.frequency == "daily":
        label = "Diario"
    elif ev.frequency == "weekly":
        label = f"Cada semana · {_WEEKDAYS[ev.day_of_week]}"
    elif ev.frequency == "monthly":
        label = f"Cada mes · día {ev.day_of_month}"
    else:
        label = "Días específicos · " + ", ".join(_WEEKDAYS_SHORT[int(d)] for d in ev.days_of_week.split(","))
    if ev.repeat_until:
        label += f" · hasta {ev.repeat_until:%d/%m/%Y}"
    return label


def _own_event(event_id: str, user: User, db: Session) -> CalendarEvent:
    try:
        uuid.UUID(event_id)
    except ValueError:
        raise HTTPException(404)
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    # Solo el dueño edita; ver un calendario ajeno es de solo lectura.
    if not ev or ev.owner_id != user.id:
        raise HTTPException(404)
    return ev


def _visible_event(event_id: str, user: User, db: Session) -> CalendarEvent:
    """Evento que `user` puede ver (y comentar): el suyo o el de alguien cuyo
    calendario puede agregar a su vista."""
    try:
        uuid.UUID(event_id)
    except ValueError:
        raise HTTPException(404)
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(404)
    if ev.owner_id != user.id and not (
        _can_subscribe(user) and calendar_members_query(user, db).filter(User.id == ev.owner_id).first()
    ):
        raise HTTPException(404)
    return ev


def _subscriptions(user: User, db: Session) -> list[CalendarSubscription]:
    return (
        db.query(CalendarSubscription)
        .options(
            joinedload(CalendarSubscription.target_user).joinedload(User.department),
            joinedload(CalendarSubscription.target_department),
        )
        .filter(CalendarSubscription.viewer_id == user.id)
        .order_by(CalendarSubscription.created_at)
        .all()
    )


def _resolve_subscriptions(user: User, subs: list[CalendarSubscription], db: Session) -> dict:
    """Lo que cada suscripción visible deja ver hoy, revalidando permisos:
    `members` (usuarios agregados, por persona o por departamento),
    `dept_ids` (departamentos completos permitidos) y `full_dept_ids` (los que
    el viewer cubre enteros, para incluir sus tareas sin asignar)."""
    person_ids = [s.target_user_id for s in subs if s.target_user_id]
    dept_ids = {s.target_department_id for s in subs if s.target_department_id}
    allowed_depts = calendar_department_ids(user, db)
    if allowed_depts is not None:
        dept_ids &= allowed_depts

    members: dict = {}
    if person_ids or dept_ids:
        conds = []
        if person_ids:
            conds.append(User.id.in_(person_ids))
        if dept_ids:
            conds.append(User.department_id.in_(dept_ids))
        members = {u.id: u for u in calendar_members_query(user, db).filter(or_(*conds)).all()}

    if user.role == ROLE_SUPERADMIN:
        full_dept_ids = set(dept_ids)
    else:
        scope = manager_scope(user, db)
        full_dept_ids = dept_ids & (scope["dept_ids"] | granted_department_ids(user))
    return {"members": members, "dept_ids": dept_ids, "full_dept_ids": full_dept_ids}


def _event_json(ev: CalendarEvent, owner: User, mine: bool, start_at: datetime, comment_count: int) -> dict:
    """Una ocurrencia (`start_at`, UTC) del evento; sin repetición, la única."""
    end_at = start_at + (ev.end_at - ev.start_at)
    return {
        # Las ocurrencias de una serie comparten `event_id` (el de la BD).
        "id": f"{ev.id}_{to_local(start_at).date().isoformat()}" if ev.frequency else str(ev.id),
        "title": ev.title,
        "start": _iso_local(start_at, ev.all_day),
        "end": _iso_local(end_at, ev.all_day),
        "allDay": ev.all_day,
        # Una serie no se arrastra: se edita desde el formulario.
        "editable": mine and not ev.frequency,
        "extendedProps": {
            "kind": "event",
            "event_id": str(ev.id),
            "mine": mine,
            "series_start": _iso_local(ev.start_at, ev.all_day),
            "series_end": _iso_local(ev.end_at, ev.all_day),
            "frequency": ev.frequency or "",
            "day_of_week": ev.day_of_week,
            "day_of_month": ev.day_of_month,
            "days_of_week": ev.days_of_week.split(",") if ev.days_of_week else [],
            "repeat_until": ev.repeat_until.isoformat() if ev.repeat_until else "",
            "repeat_label": _repeat_label(ev),
            "comment_count": comment_count,
            "color": ev.color,
            "description": ev.description or "",
            "location": ev.location or "",
            "reminder_minutes": ev.reminder_minutes,
            "owner_name": _display_name(owner),
            "owner_initials": "" if mine else _initials(owner),
        },
    }


def _task_color(task: Task) -> str:
    tag = next((tt.tag for tt in task.tags if tt.tag), None)
    return tag.color if tag else _PRIORITY_COLORS.get(task.priority, "gray")


def _follow_up_json(f: TaskFollowUp, owner: User, mine: bool) -> dict:
    task = f.task
    return {
        "id": f"followup-{f.id}",
        "title": task.title,
        "start": _iso_local(f.start_at, False),
        "end": _iso_local(f.end_at, False),
        "allDay": False,
        "editable": False,
        "url": f"/tasks/{task.id}",
        "extendedProps": {
            "kind": "followup",
            "color": _task_color(task),
            "done": task.status == TASK_DONE or f.done_at is not None,
            "follow_up_done": f.done_at is not None,
            "status": _TASK_STATUS_LABELS.get(task.status, task.status),
            "due_date": f"{task.due_date:%d/%m/%Y}" if task.due_date else "",
            "department": task.department.name if task.department else "",
            "owner_name": _display_name(owner),
            "owner_initials": "" if mine else _initials(owner),
            "description": f.note or "",
        },
    }


def _task_json(task: Task, user: User) -> dict:
    assignee_is_me = task.assigned_to == user.id
    return {
        "id": f"task-{task.id}",
        "title": task.title,
        "start": task.due_date.isoformat(),
        "allDay": True,
        "editable": False,
        "url": f"/tasks/{task.id}",
        "extendedProps": {
            "kind": "task",
            "color": _task_color(task),
            "done": task.status == TASK_DONE,
            "status": _TASK_STATUS_LABELS.get(task.status, task.status),
            "priority": _PRIORITY_LABELS.get(task.priority, task.priority),
            "department": task.department.name if task.department else "",
            "owner_name": _display_name(task.assignee) or "Sin asignar",
            "owner_initials": "" if assignee_is_me else (_initials(task.assignee) if task.assignee else "?"),
            "description": task.description or "",
        },
    }


def _project_json(project: Project) -> dict:
    return {
        "id": f"project-{project.id}",
        "title": project.name,
        "start": project.end_date.isoformat(),
        "allDay": True,
        "editable": False,
        "url": f"/projects/{project.id}",
        "extendedProps": {
            "kind": "project",
            "color": "purple",
            "status": _PROJECT_STATUS_LABELS.get(project.status, project.status),
            "department": project.department.name if project.department else "",
            "owner_name": _display_name(project.created_by_user),
            "owner_initials": "",
            "description": project.description or "",
        },
    }


# ── Page ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    date: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    subs = _subscriptions(current_user, db)
    member_options, department_options = [], []
    sub_rows = []
    if _can_subscribe(current_user):
        member_options = (
            calendar_members_query(current_user, db)
            .options(joinedload(User.department))
            .order_by(User.name, User.email)
            .all()
        )
        allowed_depts = calendar_department_ids(current_user, db)
        dq = db.query(Department).order_by(Department.name)
        if allowed_depts is not None:
            dq = dq.filter(Department.id.in_(allowed_depts)) if allowed_depts else dq.filter(Department.id.is_(None))
        department_options = dq.all()

        member_ids = {u.id for u in member_options}
        dept_option_ids = {d.id for d in department_options}
        for s in subs:
            if s.target_user_id:
                label = _display_name(s.target_user)
                detail = s.target_user.department.name if s.target_user and s.target_user.department else ""
                in_scope = s.target_user_id in member_ids
            else:
                label = s.target_department.name if s.target_department else ""
                detail = "Departamento completo"
                in_scope = s.target_department_id in dept_option_ids
            sub_rows.append({"sub": s, "label": label, "detail": detail, "in_scope": in_scope})

    subscribed_users = {s.target_user_id for s in subs if s.target_user_id}
    subscribed_depts = {s.target_department_id for s in subs if s.target_department_id}

    try:
        initial_date = datetime.strptime(date, "%Y-%m-%d").date().isoformat() if date else ""
    except ValueError:
        initial_date = ""

    return templates.TemplateResponse(
        request,
        "calendar/index.html",
        {
            "current_user": current_user,
            "can_subscribe": _can_subscribe(current_user),
            # Derivar un evento en tarea recurrente: solo encargados (como /tasks/recurring/).
            "can_recurring": is_manager(current_user),
            "subscriptions": sub_rows,
            "member_options": [u for u in member_options if u.id not in subscribed_users],
            "department_options": [d for d in department_options if d.id not in subscribed_depts],
            "max_subscriptions": CALENDAR_MAX_SUBSCRIPTIONS,
            "colors": TASK_TAG_COLORS,
            "reminder_options": CALENDAR_REMINDER_OPTIONS,
            "initial_date": initial_date,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


# ── Feed ──────────────────────────────────────────────────────────────────────

@router.get("/events")
def calendar_events(
    start: str,
    end: str,
    work: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    range_start_local, range_end_local = _parse_local(start), _parse_local(end)
    if range_end_local <= range_start_local or range_end_local - range_start_local > timedelta(days=62):
        raise HTTPException(422, "Rango inválido")
    range_start, range_end = to_utc(range_start_local), to_utc(range_end_local)

    subs = [s for s in _subscriptions(current_user, db) if s.is_visible] if _can_subscribe(current_user) else []
    resolved = _resolve_subscriptions(current_user, subs, db) if subs else {
        "members": {}, "dept_ids": set(), "full_dept_ids": set(),
    }
    owners = {current_user.id: current_user, **resolved["members"]}

    # Los repetidos se traen por inicio y fin de repetición (con holgura para
    # ocurrencias de varios días que empiezan antes del rango) y se expanden aquí.
    events = (
        db.query(CalendarEvent)
        .filter(
            CalendarEvent.owner_id.in_(list(owners)),
            CalendarEvent.start_at < range_end,
            or_(
                and_(CalendarEvent.frequency.is_(None), CalendarEvent.end_at > range_start),
                and_(
                    CalendarEvent.frequency.isnot(None),
                    or_(
                        CalendarEvent.repeat_until.is_(None),
                        CalendarEvent.repeat_until >= range_start_local.date() - timedelta(days=62),
                    ),
                ),
            ),
        )
        .order_by(CalendarEvent.start_at)
        .all()
    )
    comment_counts = dict(
        db.query(CalendarEventComment.event_id, func.count())
        .filter(CalendarEventComment.event_id.in_([ev.id for ev in events]))
        .group_by(CalendarEventComment.event_id)
        .all()
    ) if events else {}
    out = []
    for ev in events:
        duration = ev.end_at - ev.start_at
        from_day = to_local(range_start - duration).date()
        for start_at in iter_occurrences(ev, from_day, range_end_local.date()):
            if start_at < range_end and start_at + duration > range_start:
                out.append(_event_json(
                    ev, owners[ev.owner_id], ev.owner_id == current_user.id, start_at, comment_counts.get(ev.id, 0),
                ))

    # Seguimientos: en el calendario del responsable, sin depender de `work`
    # (son tiempo reservado, como una cita).
    responsible = func.coalesce(Task.assigned_to, Task.created_by)
    follow_ups = (
        db.query(TaskFollowUp)
        .join(TaskFollowUp.task)
        .options(
            contains_eager(TaskFollowUp.task).joinedload(Task.department),
            contains_eager(TaskFollowUp.task).joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
        )
        .filter(
            responsible.in_(list(owners)),
            Task.archived_at.is_(None),
            TaskFollowUp.start_at < range_end,
            TaskFollowUp.end_at > range_start,
        )
        .all()
    )
    for f in follow_ups:
        owner_id = f.task.assigned_to or f.task.created_by
        out.append(_follow_up_json(f, owners[owner_id], owner_id == current_user.id))

    if work:
        first_day, last_day = range_start_local.date(), range_end_local.date()
        task_conds = [Task.assigned_to.in_(list(owners))]
        if resolved["full_dept_ids"]:
            task_conds.append(
                and_(Task.department_id.in_(resolved["full_dept_ids"]), Task.assigned_to.is_(None))
            )
        tasks = (
            db.query(Task)
            .options(
                joinedload(Task.assignee),
                joinedload(Task.department),
                joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
            )
            .filter(
                Task.due_date.isnot(None),
                Task.due_date >= first_day,
                Task.due_date < last_day,
                Task.archived_at.is_(None),
                or_(*task_conds),
            )
            .all()
        )
        out += [_task_json(t, current_user) for t in tasks]

        # Proyectos: no tienen asignado, así que solo los propios (creados por
        # mí o de mi departamento) y los de departamentos agregados completos.
        project_conds = [Project.created_by == current_user.id]
        if current_user.department_id:
            project_conds.append(Project.department_id == current_user.department_id)
        if resolved["dept_ids"]:
            project_conds.append(Project.department_id.in_(resolved["dept_ids"]))
        visible = visible_projects_filter(current_user, db)
        if visible is not None:
            project_conds = [and_(visible, or_(*project_conds))]
        projects = (
            db.query(Project)
            .options(joinedload(Project.department), joinedload(Project.created_by_user))
            .filter(
                Project.end_date.isnot(None),
                Project.end_date >= first_day,
                Project.end_date < last_day,
                Project.status != PROJECT_COMPLETED,
                or_(*project_conds),
            )
            .all()
        )
        out += [_project_json(p) for p in projects]

    return JSONResponse(out)


# ── Events CRUD (solo el dueño) ───────────────────────────────────────────────

def _clean_color(color: str) -> str:
    return color if color in TASK_TAG_COLORS else "blue"


@router.post("/events")
def create_event(
    request: Request,
    title: str = Form(...),
    start: str = Form(...),
    end: str = Form(""),
    all_day: bool = Form(False),
    color: str = Form("blue"),
    location: str = Form(""),
    description: str = Form(""),
    reminder_minutes: str = Form(""),
    repeat: bool = Form(False),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    repeat_until: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")

    ev = CalendarEvent(
        owner_id=current_user.id,
        title=title,
        color=_clean_color(color),
        location=location.strip()[:255] or None,
        description=description.strip() or None,
        reminder_minutes=_parse_reminder(reminder_minutes),
    )
    _apply_schedule(ev, start, end, all_day)
    if _apply_recurrence(ev, repeat, frequency, day_of_week, day_of_month, days_of_week, repeat_until):
        _refresh_reminder(ev)
    db.add(ev)
    db.commit()
    audit.log_action(
        "calendar_event_create", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
    )
    return {"ok": True, "id": str(ev.id)}


@router.post("/events/{event_id}/edit")
def edit_event(
    event_id: str,
    request: Request,
    title: str = Form(...),
    start: str = Form(...),
    end: str = Form(""),
    all_day: bool = Form(False),
    color: str = Form("blue"),
    location: str = Form(""),
    description: str = Form(""),
    reminder_minutes: str = Form(""),
    repeat: bool = Form(False),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    repeat_until: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _own_event(event_id, current_user, db)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")

    ev.title = title
    ev.color = _clean_color(color)
    ev.location = location.strip()[:255] or None
    ev.description = description.strip() or None
    new_reminder = _parse_reminder(reminder_minutes)
    reminder_changed = new_reminder != ev.reminder_minutes
    ev.reminder_minutes = new_reminder
    _apply_schedule(ev, start, end, all_day)
    recurrence_changed = _apply_recurrence(
        ev, repeat, frequency, day_of_week, day_of_month, days_of_week, repeat_until,
    )
    if reminder_changed or recurrence_changed:
        _refresh_reminder(ev)
    db.commit()
    audit.log_action(
        "calendar_event_update", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
    )
    return {"ok": True}


@router.post("/events/{event_id}/move")
def move_event(
    event_id: str,
    start: str = Form(...),
    end: str = Form(""),
    all_day: bool = Form(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Arrastrar o redimensionar en el calendario."""
    if not current_user:
        raise HTTPException(401)
    ev = _own_event(event_id, current_user, db)
    if ev.frequency:
        raise HTTPException(422, "Un evento que se repite se cambia desde Editar")
    _apply_schedule(ev, start, end, all_day)
    db.commit()
    return {"ok": True}


@router.post("/events/{event_id}/delete")
def delete_event(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _own_event(event_id, current_user, db)
    title = ev.title
    db.delete(ev)
    db.commit()
    audit.log_action(
        "calendar_event_delete", user=current_user, request=request,
        resource_type="calendar_event", resource_id=event_id, resource_name=title,
    )
    return {"ok": True}


# ── Comentarios (dueño y quien ve su calendario) ──────────────────────────────

def _comment_json(c: CalendarEventComment, user: User, ev: CalendarEvent) -> dict:
    return {
        "id": str(c.id),
        "content": c.content,
        "author": _display_name(c.user),
        "initials": _initials(c.user),
        "created_at": to_local(c.created_at).isoformat(timespec="minutes"),
        "edited": c.edited,
        # Edita solo el autor; borra el autor o el dueño del evento.
        "can_edit": c.user_id == user.id,
        "can_delete": c.user_id == user.id or ev.owner_id == user.id,
    }


@router.get("/events/{event_id}/comments")
def list_comments(
    event_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    comments = (
        db.query(CalendarEventComment)
        .options(joinedload(CalendarEventComment.user))
        .filter(CalendarEventComment.event_id == ev.id)
        .order_by(CalendarEventComment.created_at)
        .all()
    )
    return JSONResponse([_comment_json(c, current_user, ev) for c in comments])


@router.post("/events/{event_id}/comments")
def add_comment(
    event_id: str,
    request: Request,
    content: str = Form(...),
    date: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    content = content.strip()[:5000]
    if not content:
        raise HTTPException(422, "Escribe un comentario")

    comment = CalendarEventComment(event_id=ev.id, user_id=current_user.id, content=content)
    db.add(comment)
    db.commit()
    db.refresh(comment)

    # Avisa al dueño y a quienes ya comentaron (menos a quien comenta). `date`
    # es la ocurrencia desde la que se comentó, para abrir el calendario ahí.
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        day = to_local(ev.start_at).date()
    followers = {ev.owner_id} | {
        uid for (uid,) in db.query(CalendarEventComment.user_id).filter(CalendarEventComment.event_id == ev.id)
    }
    actor = _display_name(current_user)
    for uid in followers - {current_user.id}:
        realtime.notify_user(
            uid,
            title=f"{actor} comentó en «{ev.title}»",
            body=content[:120],
            url=f"/calendar/?date={day.isoformat()}",
            kind="calendar_comment",
        )
    audit.log_action(
        "calendar_event_comment", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
        details=content[:80],
    )
    return _comment_json(comment, current_user, ev)


def _event_comment(ev: CalendarEvent, comment_id: str, db: Session) -> CalendarEventComment:
    try:
        uuid.UUID(comment_id)
    except ValueError:
        raise HTTPException(404)
    comment = db.query(CalendarEventComment).filter(
        CalendarEventComment.id == comment_id, CalendarEventComment.event_id == ev.id,
    ).first()
    if not comment:
        raise HTTPException(404)
    return comment


@router.post("/events/{event_id}/comments/{comment_id}/edit")
def edit_comment(
    event_id: str,
    comment_id: str,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    comment = _event_comment(ev, comment_id, db)
    if comment.user_id != current_user.id:
        raise HTTPException(403)
    content = content.strip()[:5000]
    if not content:
        raise HTTPException(422, "Escribe un comentario")
    if content != comment.content:
        comment.content = content
        comment.edited = True
        db.commit()
    return _comment_json(comment, current_user, ev)


@router.post("/events/{event_id}/comments/{comment_id}/delete")
def delete_comment(
    event_id: str,
    comment_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    comment = _event_comment(ev, comment_id, db)
    if comment.user_id != current_user.id and ev.owner_id != current_user.id:
        raise HTTPException(403)
    db.delete(comment)
    db.commit()
    return {"ok": True}


# ── Subscriptions (calendarios agregados) ─────────────────────────────────────

@router.post("/subscriptions")
def add_subscription(
    request: Request,
    target_user_id: str = Form(""),
    target_department_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not _can_subscribe(current_user):
        raise HTTPException(403)
    if bool(target_user_id) == bool(target_department_id):
        raise HTTPException(422, "Elige una persona o un departamento")

    count = db.query(CalendarSubscription).filter(CalendarSubscription.viewer_id == current_user.id).count()
    if count >= CALENDAR_MAX_SUBSCRIPTIONS:
        raise HTTPException(422, f"Máximo {CALENDAR_MAX_SUBSCRIPTIONS} calendarios agregados")

    try:
        target_id = uuid.UUID(target_user_id or target_department_id)
    except ValueError:
        raise HTTPException(404)

    if target_user_id:
        target = calendar_members_query(current_user, db).filter(User.id == target_id).first()
        if not target:
            raise HTTPException(403)
        exists = CalendarSubscription.target_user_id == target_id
        sub = CalendarSubscription(viewer_id=current_user.id, target_user_id=target_id)
        name = _display_name(target)
    else:
        allowed = calendar_department_ids(current_user, db)
        dept = db.query(Department).filter(Department.id == target_id).first()
        if not dept or (allowed is not None and target_id not in allowed):
            raise HTTPException(403)
        exists = CalendarSubscription.target_department_id == target_id
        sub = CalendarSubscription(viewer_id=current_user.id, target_department_id=target_id)
        name = dept.name

    if not db.query(CalendarSubscription).filter(CalendarSubscription.viewer_id == current_user.id, exists).first():
        db.add(sub)
        db.commit()
        audit.log_action(
            "calendar_subscription_add", user=current_user, request=request,
            resource_type="calendar_subscription", resource_id=sub.id, resource_name=name,
        )
    return RedirectResponse("/calendar/", status_code=302)


def _own_subscription(sub_id: str, user: User, db: Session) -> CalendarSubscription:
    try:
        uuid.UUID(sub_id)
    except ValueError:
        raise HTTPException(404)
    sub = db.query(CalendarSubscription).filter(
        CalendarSubscription.id == sub_id, CalendarSubscription.viewer_id == user.id,
    ).first()
    if not sub:
        raise HTTPException(404)
    return sub


@router.post("/subscriptions/{sub_id}/toggle")
def toggle_subscription(
    sub_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    sub = _own_subscription(sub_id, current_user, db)
    sub.is_visible = not sub.is_visible
    db.commit()
    return {"ok": True, "is_visible": sub.is_visible}


@router.post("/subscriptions/{sub_id}/delete")
def delete_subscription(
    sub_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    sub = _own_subscription(sub_id, current_user, db)
    db.delete(sub)
    db.commit()
    audit.log_action(
        "calendar_subscription_remove", user=current_user, request=request,
        resource_type="calendar_subscription", resource_id=sub_id,
    )
    return RedirectResponse("/calendar/", status_code=302)
