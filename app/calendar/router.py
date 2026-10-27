"""Calendario personal: citas y actividades de cada usuario, más los calendarios
ajenos que tiene agregados (admin → su gente; superadmin → cualquiera) y,
opcionalmente, las fechas límite de tareas y proyectos.

Los seguimientos de tareas (TaskFollowUp) salen siempre, como bloques con hora,
en el calendario de su responsable (el asignado, o el creador si no hay).

El dueño puede compartir un evento con otras personas: les aparece en su
calendario (solo lectura), lo comentan y reciben sus avisos y recordatorios.

Los eventos pueden repetirse con los mismos criterios que las tareas
recurrentes (diario, semanal, mensual, días específicos); las ocurrencias se
calculan al consultar y la serie se edita completa. Los eventos aceptan
comentarios del dueño y de quien puede ver su calendario.

Fechas: se guardan en UTC; hacia y desde el navegador viajan en hora local del
negocio sin offset (FullCalendar corre con timeZone 'UTC' para mostrarlas tal
cual, igual que el resto de la app muestra todo fijo en hora de Monterrey).
"""
import re
import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from app import audit, storage
from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.calendar.reminders import STALE_AFTER, iter_occurrences, next_remind_at, to_local, to_utc
from app.database import get_db
from app.models import (
    CalendarEvent, CalendarEventAttachment, CalendarEventComment, CalendarEventShare, CalendarSubscription, Department, Project, Task, TaskFollowUp,
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
        # Cada ocurrencia dura a lo más un día; la serie termina en "Hasta".
        if ev.end_at - ev.start_at > timedelta(days=1):
            raise HTTPException(
                422, "Un evento que se repite dura un solo día; usa «Hasta» para indicar cuándo termina la repetición",
            )
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
    """Evento que `user` puede ver (y comentar): el suyo, uno compartido con él
    o el de alguien cuyo calendario puede agregar a su vista."""
    try:
        uuid.UUID(event_id)
    except ValueError:
        raise HTTPException(404)
    ev = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(404)
    if ev.owner_id != user.id and not _is_shared_with(ev, user, db) and not (
        _can_subscribe(user) and calendar_members_query(user, db).filter(
            or_(User.id == ev.owner_id, User.id.in_([sh.user_id for sh in ev.shares]))
        ).first()
    ):
        raise HTTPException(404)
    return ev


def _is_shared_with(ev: CalendarEvent, user: User, db: Session) -> bool:
    return db.query(CalendarEventShare).filter(
        CalendarEventShare.event_id == ev.id, CalendarEventShare.user_id == user.id,
    ).first() is not None


def _share_candidates(user: User, db: Session) -> list[User]:
    """Cualquier persona activa (el selector filtra por departamento y nombre)."""
    return (
        db.query(User).options(joinedload(User.department))
        .filter(User.is_active.is_(True), User.id != user.id)
        .order_by(User.name, User.email).all()
    )


def _set_shares(ev: CalendarEvent, user_ids: list[str], db: Session) -> list:
    """Deja el evento compartido exactamente con `user_ids` (activos, sin el
    dueño). Devuelve los ids recién agregados, para avisarles tras el commit."""
    try:
        wanted = {uuid.UUID(uid) for uid in user_ids if uid} - {ev.owner_id}
    except ValueError:
        raise HTTPException(422, "Persona inválida")
    valid = {
        row[0] for row in db.query(User.id).filter(User.id.in_(wanted), User.is_active.is_(True)).all()
    } if wanted else set()
    current = {sh.user_id: sh for sh in ev.shares}
    for uid, sh in current.items():
        if uid not in valid:
            ev.shares.remove(sh)
    added = valid - set(current)
    for uid in added:
        ev.shares.append(CalendarEventShare(user_id=uid))
    return list(added)


def _when(ev: CalendarEvent) -> str:
    start = to_local(ev.start_at)
    label = f"{start:%d/%m/%Y}" + ("" if ev.all_day else f" {start:%H:%M}")
    return f"{label} · {_repeat_label(ev)}" if ev.frequency else label


def _notify_shared(ev: CalendarEvent, user_ids, actor: User, title: str) -> None:
    for uid in user_ids:
        realtime.notify_user(
            uid, title=title, body=f"{ev.title} · {_when(ev)}",
            url=f"/calendar/?date={to_local(ev.start_at).date().isoformat()}&event={ev.id}", kind="calendar_shared",
        )


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


def _event_json(
    ev: CalendarEvent, owner: User, mine: bool, start_at: datetime, comment_count: int, attachment_count: int, user: User,
) -> dict:
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
            "attachment_count": attachment_count,
            "color": ev.color,
            "description": ev.description or "",
            "location": ev.location or "",
            "reminder_minutes": ev.reminder_minutes,
            "owner_name": _display_name(owner),
            "owner_initials": "" if mine else _initials(owner),
            "shared_with": [{"id": str(sh.user_id), "name": _display_name(sh.user)} for sh in ev.shares],
            "shared_to_me": any(sh.user_id == user.id for sh in ev.shares),
        },
    }


def _task_color(task: Task) -> str:
    tag = next((tt.tag for tt in task.tags if tt.tag), None)
    return tag.color if tag else _PRIORITY_COLORS.get(task.priority, "gray")


def _follow_up_json(f: TaskFollowUp, owner: User, mine: bool, creator: User | None = None) -> dict:
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
            # Quien lo agendó, cuando no es el responsable (p.ej. el encargado que
            # le reservó tiempo a alguien de su equipo).
            "created_by_name": _display_name(creator) if creator and creator.id != owner.id else "",
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
    event: str = "",
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
    try:
        initial_event = str(uuid.UUID(event)) if event else ""
    except ValueError:
        initial_event = ""

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
            "share_candidates": _share_candidates(current_user, db),
            "all_departments": db.query(Department).order_by(Department.name).all(),
            "reminder_options": CALENDAR_REMINDER_OPTIONS,
            "initial_date": initial_date,
            # Evento a abrir al cargar (desde una notificación).
            "initial_event": initial_event,
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
    # Compartidos conmigo y con las personas agregadas (sin duplicar: un evento
    # es una sola fila aunque varios lo tengan).
    shared_ids = db.query(CalendarEventShare.event_id).filter(CalendarEventShare.user_id.in_(list(owners)))
    events = (
        db.query(CalendarEvent)
        .options(
            joinedload(CalendarEvent.owner),
            joinedload(CalendarEvent.shares).joinedload(CalendarEventShare.user),
        )
        .filter(
            or_(CalendarEvent.owner_id.in_(list(owners)), CalendarEvent.id.in_(shared_ids)),
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
    events = list(dict.fromkeys(events))  # joinedload de colección: sin duplicados
    comment_counts = dict(
        db.query(CalendarEventComment.event_id, func.count())
        .filter(CalendarEventComment.event_id.in_([ev.id for ev in events]))
        .group_by(CalendarEventComment.event_id)
        .all()
    ) if events else {}
    attachment_counts = dict(
        db.query(CalendarEventAttachment.event_id, func.count())
        .filter(CalendarEventAttachment.event_id.in_([ev.id for ev in events]))
        .group_by(CalendarEventAttachment.event_id)
        .all()
    ) if events else {}
    out = []
    for ev in events:
        duration = ev.end_at - ev.start_at
        from_day = to_local(range_start - duration).date()
        for start_at in iter_occurrences(ev, from_day, range_end_local.date()):
            if start_at < range_end and start_at + duration > range_start:
                out.append(_event_json(
                    ev, owners.get(ev.owner_id) or ev.owner, ev.owner_id == current_user.id, start_at,
                    comment_counts.get(ev.id, 0), attachment_counts.get(ev.id, 0), current_user,
                ))

    # Seguimientos: en el calendario del responsable y de quien los creó, sin
    # depender de `work` (son tiempo reservado, como una cita). Igual para las
    # personas agregadas: también los que ellas agendaron en tareas de otros.
    responsible = func.coalesce(Task.assigned_to, Task.created_by)
    follow_ups = (
        db.query(TaskFollowUp)
        .join(TaskFollowUp.task)
        .options(
            contains_eager(TaskFollowUp.task).joinedload(Task.department),
            contains_eager(TaskFollowUp.task).joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
            contains_eager(TaskFollowUp.task).joinedload(Task.assignee),
            contains_eager(TaskFollowUp.task).joinedload(Task.created_by_user),
        )
        .filter(
            or_(responsible.in_(list(owners)), TaskFollowUp.created_by.in_(list(owners))),
            Task.archived_at.is_(None),
            TaskFollowUp.start_at < range_end,
            TaskFollowUp.end_at > range_start,
        )
        .all()
    )
    missing = {f.created_by for f in follow_ups if f.created_by and f.created_by not in owners}
    creators = {**owners, **({u.id: u for u in db.query(User).filter(User.id.in_(missing))} if missing else {})}
    for f in follow_ups:
        owner = f.task.assignee or f.task.created_by_user
        out.append(_follow_up_json(f, owner, owner.id == current_user.id, creators.get(f.created_by)))

    if work:
        first_day, last_day = range_start_local.date(), range_end_local.date()
        task_conds = [Task.assigned_to.in_(list(owners)), Task.created_by == current_user.id]
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

    # Una entrada por id aunque llegue por varias vías (mía, compartida, agregada).
    return JSONResponse(list({item["id"]: item for item in out}.values()))


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
    shared_with: list[str] = Form([]),
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
    added = _set_shares(ev, shared_with, db)
    db.add(ev)
    db.commit()
    _notify_shared(ev, added, current_user, f"{_display_name(current_user)} compartió un evento contigo")
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
    shared_with: list[str] = Form([]),
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
    added = _set_shares(ev, shared_with, db)
    db.commit()
    _notify_shared(ev, added, current_user, f"{_display_name(current_user)} compartió un evento contigo")
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
    shared = [sh.user_id for sh in ev.shares]
    file_keys = [a.file_key for a in ev.attachments]
    _notify_shared(ev, shared, current_user, f"{_display_name(current_user)} canceló un evento")
    db.delete(ev)
    db.commit()
    for key in file_keys:
        try:
            storage.delete_evidence(key)
        except Exception:
            pass
    audit.log_action(
        "calendar_event_delete", user=current_user, request=request,
        resource_type="calendar_event", resource_id=event_id, resource_name=title,
    )
    return {"ok": True}


@router.post("/events/{event_id}/leave")
def leave_event(
    event_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Quien recibió un evento compartido lo quita de su calendario."""
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    db.query(CalendarEventShare).filter(
        CalendarEventShare.event_id == ev.id, CalendarEventShare.user_id == current_user.id,
    ).delete()
    db.commit()
    return {"ok": True}


# ── Comentarios (dueño, compartidos y quien ve su calendario) ─────────────────

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

    # Avisa al dueño, a los compartidos y a quienes ya comentaron (menos a
    # quien comenta). `date`
    # es la ocurrencia desde la que se comentó, para abrir el calendario ahí.
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        day = to_local(ev.start_at).date()
    followers = {ev.owner_id} | {sh.user_id for sh in ev.shares} | {
        uid for (uid,) in db.query(CalendarEventComment.user_id).filter(CalendarEventComment.event_id == ev.id)
    }
    actor = _display_name(current_user)
    for uid in followers - {current_user.id}:
        realtime.notify_user(
            uid,
            title=f"{actor} comentó en «{ev.title}»",
            body=content[:120],
            url=f"/calendar/?date={day.isoformat()}&event={ev.id}",
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


# ── Archivos / evidencias (mismos permisos que los comentarios) ──────────────

def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


def _attachment_json(a: CalendarEventAttachment, user: User, ev: CalendarEvent) -> dict:
    return {
        "id": str(a.id),
        "filename": a.filename,
        "size": a.file_size,
        "is_image": a.content_type.startswith("image/"),
        "uploader": _display_name(a.uploader),
        "created_at": to_local(a.created_at).isoformat(timespec="minutes"),
        "url": f"/calendar/events/{ev.id}/attachments/{a.id}/download",
        # Borra quien lo subió o el dueño del evento.
        "can_delete": a.uploaded_by == user.id or ev.owner_id == user.id,
    }


def _event_attachment(ev: CalendarEvent, attachment_id: str, db: Session) -> CalendarEventAttachment:
    try:
        uuid.UUID(attachment_id)
    except ValueError:
        raise HTTPException(404)
    a = db.query(CalendarEventAttachment).filter(
        CalendarEventAttachment.id == attachment_id, CalendarEventAttachment.event_id == ev.id,
    ).first()
    if not a:
        raise HTTPException(404)
    return a


@router.get("/events/{event_id}/attachments")
def list_attachments(
    event_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    attachments = (
        db.query(CalendarEventAttachment)
        .options(joinedload(CalendarEventAttachment.uploader))
        .filter(CalendarEventAttachment.event_id == ev.id)
        .order_by(CalendarEventAttachment.created_at)
        .all()
    )
    return JSONResponse([_attachment_json(a, current_user, ev) for a in attachments])


@router.post("/events/{event_id}/attachments")
def upload_attachments(
    event_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    date: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    files = [f for f in files if f.filename]
    if not files:
        raise HTTPException(422, "Elige al menos un archivo")
    storage.check_upload_sizes(*files)

    added = []
    for f in files:
        content = f.file.read()
        key = f"calendar/{ev.id}/{uuid.uuid4()}_{_safe_filename(f.filename)}"
        content_type = (f.content_type or "application/octet-stream")[:100]
        storage.upload_evidence(key, content, content_type)
        a = CalendarEventAttachment(
            event_id=ev.id, uploaded_by=current_user.id, filename=f.filename[:255],
            file_key=key, content_type=content_type, file_size=len(content),
        )
        db.add(a)
        added.append(a)
    db.commit()

    # Avisa al dueño y a los compartidos (menos a quien sube), como los comentarios.
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        day = to_local(ev.start_at).date()
    actor = _display_name(current_user)
    for uid in ({ev.owner_id} | {sh.user_id for sh in ev.shares}) - {current_user.id}:
        realtime.notify_user(
            uid,
            title=f"{actor} adjuntó {'un archivo' if len(added) == 1 else f'{len(added)} archivos'} en «{ev.title}»",
            body=", ".join(a.filename for a in added)[:120],
            url=f"/calendar/?date={day.isoformat()}&event={ev.id}",
            kind="calendar_attachment",
        )
    audit.log_action(
        "calendar_attachment_upload", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
        details=f"files={len(added)} names={','.join(a.filename for a in added[:5])}",
    )
    return JSONResponse([_attachment_json(a, current_user, ev) for a in added])


@router.get("/events/{event_id}/attachments/{attachment_id}/download")
def download_attachment(
    event_id: str,
    attachment_id: str,
    inline: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    a = _event_attachment(ev, attachment_id, db)
    return RedirectResponse(storage.get_evidence_url(a.file_key, a.filename, inline=inline), status_code=302)


@router.post("/events/{event_id}/attachments/{attachment_id}/delete")
def delete_attachment(
    event_id: str,
    attachment_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    a = _event_attachment(ev, attachment_id, db)
    if a.uploaded_by != current_user.id and ev.owner_id != current_user.id:
        raise HTTPException(403)
    filename = a.filename
    try:
        storage.delete_evidence(a.file_key)
    except Exception:
        pass
    db.delete(a)
    db.commit()
    audit.log_action(
        "calendar_attachment_delete", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=filename,
    )
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
