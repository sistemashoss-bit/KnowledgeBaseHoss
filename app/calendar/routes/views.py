"""Página del calendario y feed de eventos (eventos propios, compartidos, agregados, tareas y proyectos)."""

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.calendar.reminders import iter_occurrences, to_local, to_utc
from app.database import get_db
from app.models import (
    CalendarEvent,
    CalendarEventAttachment,
    CalendarEventComment,
    CalendarEventShare,
    Department,
    Project,
    Task,
    TaskFollowUp,
    TaskTagAssignment,
    User,
    CALENDAR_MAX_SUBSCRIPTIONS,
    CALENDAR_REMINDER_OPTIONS,
    PROJECT_COMPLETED,
    TASK_TAG_COLORS,
)
from app.permissions import (
    calendar_department_ids,
    calendar_members_query,
    is_manager,
    visible_projects_filter,
)
from app.templating import templates
from app.calendar.helpers import (
    _can_subscribe,
    _display_name,
    _event_json,
    _follow_up_json,
    _is_uuid,
    _parse_local,
    _project_json,
    _resolve_subscriptions,
    _share_candidates,
    _subscriptions,
    _task_json,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def calendar_page(
    request: Request,
    date: str = "",
    event: str = "",
    new_meeting: bool = False,
    title: str = "",
    invite: list[str] = Query(default=[]),
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
            # "Programar reunión" desde un chat: formulario de reunión prellenado.
            "initial_meeting": {
                "title": title.strip()[:255],
                "invite": [uid for uid in invite if _is_uuid(uid) and uid != str(current_user.id)],
            } if new_meeting else None,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


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
