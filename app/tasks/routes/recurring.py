"""Tareas recurrentes (/tasks/recurring)."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app import audit
from app.permissions import (
    is_manager,
    branch_user_ids as _branch_user_ids,
    manager_scope as _manager_scope,
)
from app.models import (
    Department,
    Project,
    RecurringTask,
    RecurringTaskShare,
    User,
    ROLE_SUPERADMIN,
    TASK_PRIORITIES,
    RECURRENCE_FREQUENCIES,
)
from app.tasks.recurring import parse_recurrence
from app.tasks.service import (
    _assignable_users_query,
    _validate_target_scope,
    _validated_document_id,
)
from app.templating import templates
from app.tasks.helpers import (
    _accessible_documents,
    _share_candidates,
)


recurring_router = APIRouter(prefix="/tasks/recurring", tags=["recurring-tasks"])


def _assignable_department_ids(user: User, db: Session) -> set | None:
    """Departamentos que el encargado puede fijar en una plantilla.

    None = sin restricción (superadmin). Un admin acotado a sucursales
    (Comercial) fija su propio departamento."""
    scope = _manager_scope(user, db)
    if scope is None:
        return None
    dept_ids = set(scope["dept_ids"])
    if scope["branch_dept_id"]:
        dept_ids.add(scope["branch_dept_id"])
    return dept_ids


def _recurring_query(user: User, db: Session):
    q = db.query(RecurringTask).options(
        joinedload(RecurringTask.assignee),
        joinedload(RecurringTask.department),
        joinedload(RecurringTask.project),
        joinedload(RecurringTask.document),
        joinedload(RecurringTask.created_by_user),
        joinedload(RecurringTask.shares).joinedload(RecurringTaskShare.user),
    )
    scope = _manager_scope(user, db)
    if scope is None:
        return q  # superadmin ve todo
    conditions = [RecurringTask.created_by == user.id]
    if scope["dept_ids"]:
        conditions.append(RecurringTask.department_id.in_(scope["dept_ids"]))
    branch_user_ids = _branch_user_ids(scope, db)
    if branch_user_ids:
        conditions.append(RecurringTask.assigned_to.in_(branch_user_ids))
    return q.filter(or_(*conditions))


def _can_manage_recurring(user: User, rt: RecurringTask, db: Session) -> bool:
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(rt.created_by) == str(user.id):
        return True
    scope = _manager_scope(user, db)
    if scope is None:
        return True
    if scope["dept_ids"] and rt.department_id and rt.department_id in scope["dept_ids"]:
        return True
    if rt.assigned_to and rt.assigned_to in _branch_user_ids(scope, db):
        return True
    return False


def _set_recurring_shares(rt: RecurringTask, user_ids: list[str], db: Session) -> None:
    """Deja como compartidos de la plantilla exactamente a `user_ids` (activos;
    sin el creador ni el asignado, que ya siguen cada tarea generada)."""
    try:
        wanted = {uuid.UUID(uid) for uid in user_ids if uid}
    except ValueError:
        raise HTTPException(400, "Persona inválida")
    wanted -= {rt.created_by, uuid.UUID(str(rt.assigned_to)) if rt.assigned_to else None}
    valid = {
        row[0] for row in db.query(User.id).filter(User.id.in_(wanted), User.is_active == True).all()  # noqa: E712
    } if wanted else set()
    current = {s.user_id: s for s in rt.shares}
    for uid, share in current.items():
        if uid not in valid:
            rt.shares.remove(share)
    for uid in valid - set(current):
        rt.shares.append(RecurringTaskShare(user_id=uid))


@recurring_router.get("/", response_class=HTMLResponse)
def list_recurring(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    if not is_manager(current_user):
        raise HTTPException(403)

    items = _recurring_query(current_user, db).order_by(RecurringTask.created_at.desc()).all()

    # Proyectos: acotados al alcance de gestión (zona/departamento), igual que
    # antes. Departamentos (difusión) y usuarios (asignación puntual) siguen el
    # criterio nuevo: cualquier encargado difunde a cualquier depto, pero sólo
    # asigna en persona dentro del suyo propio (ver _assignable_users_query).
    dept_ids = _assignable_department_ids(current_user, db)
    proj_q = db.query(Project).order_by(Project.name)
    if dept_ids is not None:
        scoped = dept_ids or {None}  # evita IN () vacío
        proj_q = proj_q.filter(Project.department_id.in_(scoped))

    departments = db.query(Department).order_by(Department.name).all()
    users = _assignable_users_query(current_user, db).all()
    projects = proj_q.all()

    return templates.TemplateResponse(
        request,
        "tasks/recurring.html",
        {
            "current_user": current_user,
            "items": items,
            "departments": departments,
            "users": users,
            "projects": projects,
            "documents": _accessible_documents(current_user),
            # Incluye al propio encargado: puede editar una plantilla que otro
            # compartió con él, y el selector solo conserva a quien conoce.
            "share_candidates": sorted(
                _share_candidates(current_user, db) + [current_user], key=lambda u: (u.name or "", u.email),
            ),
            "priorities": TASK_PRIORITIES,
            "frequencies": RECURRENCE_FREQUENCIES,
            "is_superadmin": current_user.role == ROLE_SUPERADMIN,
            "today": date.today().isoformat(),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@recurring_router.post("/")
def create_recurring(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_id: str = Form(""),
    document_id: str = Form(""),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    start_date: str = Form(""),
    end_date: str = Form(""),
    shared_with: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not is_manager(current_user):
        raise HTTPException(403)

    _validate_target_scope(current_user, db, department_id, assigned_to)
    freq, dow, dom, custom_days = parse_recurrence(frequency, day_of_week, day_of_month, days_of_week)

    rt = RecurringTask(
        id=uuid.uuid4(),
        title=title.strip(),
        description=description.strip() or None,
        priority=priority if priority in TASK_PRIORITIES else "medium",
        department_id=department_id if department_id else None,
        assigned_to=assigned_to if assigned_to else None,
        project_id=project_id if project_id else None,
        document_id=_validated_document_id(document_id, current_user, db),
        created_by=current_user.id,
        frequency=freq,
        day_of_week=dow,
        day_of_month=dom,
        days_of_week=custom_days,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
    )
    _set_recurring_shares(rt, shared_with, db)
    db.add(rt)
    db.commit()
    audit.log_action(
        "recurring_create", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt.id, resource_name=rt.title,
        details=f"frequency={rt.frequency}",
    )
    return RedirectResponse("/tasks/recurring/", status_code=302)


@recurring_router.post("/{rt_id}/edit")
def edit_recurring(
    rt_id: str,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_id: str = Form(""),
    document_id: str = Form(""),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    start_date: str = Form(""),
    end_date: str = Form(""),
    shared_with: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    rt = db.query(RecurringTask).filter(RecurringTask.id == rt_id).first()
    if not rt:
        raise HTTPException(404)
    if not _can_manage_recurring(current_user, rt, db):
        raise HTTPException(403)

    _validate_target_scope(current_user, db, department_id, assigned_to)
    freq, dow, dom, custom_days = parse_recurrence(frequency, day_of_week, day_of_month, days_of_week)

    rt.title = title.strip()
    rt.description = description.strip() or None
    rt.priority = priority if priority in TASK_PRIORITIES else "medium"
    rt.department_id = department_id if department_id else None
    rt.assigned_to = assigned_to if assigned_to else None
    rt.project_id = project_id if project_id else None
    rt.document_id = _validated_document_id(document_id, current_user, db)
    rt.frequency = freq
    rt.day_of_week = dow
    rt.day_of_month = dom
    rt.days_of_week = custom_days
    rt.start_date = date.fromisoformat(start_date) if start_date else None
    rt.end_date = date.fromisoformat(end_date) if end_date else None
    _set_recurring_shares(rt, shared_with, db)
    db.commit()
    audit.log_action(
        "recurring_update", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt.id, resource_name=rt.title,
    )
    return RedirectResponse("/tasks/recurring/", status_code=302)


@recurring_router.post("/{rt_id}/toggle", response_class=HTMLResponse)
def toggle_recurring(
    rt_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    rt = db.query(RecurringTask).filter(RecurringTask.id == rt_id).first()
    if not rt:
        raise HTTPException(404)
    if not _can_manage_recurring(current_user, rt, db):
        raise HTTPException(403)
    rt.is_active = not rt.is_active
    db.commit()
    audit.log_action(
        "recurring_toggle", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt.id, resource_name=rt.title,
        details=f"is_active={rt.is_active}",
    )
    return HTMLResponse(headers={"HX-Redirect": "/tasks/recurring/"})


@recurring_router.post("/{rt_id}/delete", response_class=HTMLResponse)
def delete_recurring(
    rt_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    rt = db.query(RecurringTask).filter(RecurringTask.id == rt_id).first()
    if not rt:
        raise HTTPException(404)
    if not _can_manage_recurring(current_user, rt, db):
        raise HTTPException(403)
    title = rt.title
    db.delete(rt)
    db.commit()
    audit.log_action(
        "recurring_delete", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt_id, resource_name=title,
    )
    return HTMLResponse(headers={"HX-Redirect": "/tasks/recurring/"})
