"""Funciones y constantes compartidas por las rutas del calendario (app/calendar/routes/)."""

import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.calendar.reminders import STALE_AFTER, next_remind_at, to_local, to_utc
from app.models import (
    CalendarEvent,
    CalendarEventShare,
    CalendarSubscription,
    Project,
    Task,
    TaskFollowUp,
    User,
    CALENDAR_REMINDER_OPTIONS,
    ROLE_SUPERADMIN,
    TASK_DONE,
)
from app.messaging import realtime
from app.permissions import (
    calendar_department_ids,
    calendar_members_query,
    granted_department_ids,
    has_oversight,
    manager_scope,
)
from app.tasks.recurring import parse_recurrence


_PRIORITY_COLORS = {"low": "gray", "medium": "blue", "high": "orange", "urgent": "red"}


_TASK_STATUS_LABELS = {
    "pending": "Pendiente", "in_progress": "En progreso", "review": "En revisión", "done": "Listo",
}


_PRIORITY_LABELS = {"low": "Baja", "medium": "Media", "high": "Alta", "urgent": "Urgente"}


_PROJECT_STATUS_LABELS = {"draft": "Borrador", "active": "Activo", "on_hold": "En pausa", "completed": "Completado"}


_WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


_WEEKDAYS_SHORT = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


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


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


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
            url=f"/calendar/?date={to_local(ev.start_at).date().isoformat()}&event={ev.id}",
            kind="calendar_meeting" if ev.is_meeting else "calendar_shared",
        )


def _invite_title(ev: CalendarEvent, actor: User) -> str:
    who = _display_name(actor)
    return f"{who} te invitó a una reunión" if ev.is_meeting else f"{who} compartió un evento contigo"


def _schedule_key(ev: CalendarEvent) -> tuple:
    """Lo que, si cambia, obliga a los invitados a confirmar de nuevo."""
    return (
        ev.start_at, ev.end_at, ev.all_day, ev.frequency, ev.day_of_week, ev.day_of_month,
        ev.days_of_week, ev.repeat_until,
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
            "shared_with": [
                {"id": str(sh.user_id), "name": _display_name(sh.user), "response": sh.response or ""}
                for sh in ev.shares
            ],
            "shared_to_me": any(sh.user_id == user.id for sh in ev.shares),
            "is_meeting": ev.is_meeting,
            "my_response": next((sh.response or "" for sh in ev.shares if sh.user_id == user.id), ""),
            "meeting_url": f"/calendar/meet/{ev.id}" if ev.is_meeting else "",
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


def _notify_meeting_owner(ev: CalendarEvent, actor: User, what: str) -> None:
    realtime.notify_user(
        ev.owner_id, title=f"{_display_name(actor)} {what}", body=f"{ev.title} · {_when(ev)}",
        url=f"/calendar/?date={to_local(ev.start_at).date().isoformat()}&event={ev.id}", kind="calendar_rsvp",
    )
