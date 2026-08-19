import re
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import false, or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app import audit, storage
from app.messaging import realtime
from app.models import (
    Branch, Department, Project, RecurringTask, Task, TaskComment, TaskEvidence, User,
    ROLE_SUPERADMIN, ROLE_ADMIN,
    TASK_STATUSES, TASK_PRIORITIES,
    RECURRENCE_FREQUENCIES, FREQ_WEEKLY, FREQ_MONTHLY,
)
from app.templating import templates

MAX_EVIDENCE_BYTES = 50 * 1024 * 1024  # 50 MB


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ── Visibility helpers ────────────────────────────────────────────────────────

def _tasks_query(user: User, db: Session):
    q = db.query(Task).options(
        joinedload(Task.assignee),
        joinedload(Task.created_by_user),
        joinedload(Task.department),
        joinedload(Task.project),
    )
    if user.role == ROLE_SUPERADMIN:
        return q
    conditions = [
        Task.created_by == user.id,
        Task.assigned_to == user.id,
    ]
    if user.department_id:
        conditions.append(Task.department_id == user.department_id)
    return q.filter(or_(*conditions))


def _can_edit_task(user: User, task: Task) -> bool:
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(task.created_by) == str(user.id):
        return True
    if user.role == ROLE_ADMIN and task.department_id and str(task.department_id) == str(user.department_id):
        return True
    return False


def _can_update_status(user: User, task: Task) -> bool:
    if _can_edit_task(user, task):
        return True
    return task.assigned_to and str(task.assigned_to) == str(user.id)


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def list_tasks(
    request: Request,
    tab: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    is_admin = current_user.role in (ROLE_ADMIN, ROLE_SUPERADMIN)

    # Available tabs — "dept" only for admins/superadmins.
    valid_tabs = ["assigned", "created"] + (["dept"] if is_admin else [])
    if tab not in valid_tabs:
        tab = "assigned"

    base = db.query(Task).options(
        joinedload(Task.assignee),
        joinedload(Task.created_by_user),
        joinedload(Task.department),
        joinedload(Task.project),
    )

    if tab == "assigned":
        q = base.filter(Task.assigned_to == current_user.id)
        can_drag = True  # the assignee may move their own tasks between statuses
    elif tab == "created":
        q = base.filter(Task.created_by == current_user.id)
        can_drag = False
    else:  # dept
        # Mismo organigrama que las plantillas recurrentes:
        # superadmin → todo; admin sin zona → su departamento; admin con zona →
        # las tareas asignadas a personas de las sucursales de sus zonas.
        scope = _manager_scope(current_user, db)
        if scope is None:
            q = base  # superadmin ve todos los departamentos
        else:
            conds = []
            if scope["dept_ids"]:
                conds.append(Task.department_id.in_(scope["dept_ids"]))
            branch_user_ids = _branch_user_ids(scope["branch_ids"], db)
            if branch_user_ids:
                conds.append(Task.assigned_to.in_(branch_user_ids))
            q = base.filter(or_(*conds)) if conds else base.filter(false())
        can_drag = False

    tasks = q.order_by(Task.created_at.desc()).all()

    # Group into Kanban columns keyed by status.
    columns = {s: [] for s in TASK_STATUSES}
    for t in tasks:
        columns.setdefault(t.status, []).append(t)

    # Data for create form
    departments = db.query(Department).order_by(Department.name).all()
    users = db.query(User).filter(User.is_active == True).order_by(User.email).all()
    projects = db.query(Project).order_by(Project.name).all()

    csrf = generate_csrf_token(str(current_user.id))
    return templates.TemplateResponse(
        request,
        "tasks/list.html",
        {
            "current_user": current_user,
            "columns": columns,
            "tasks": tasks,
            "total": len(tasks),
            "tab": tab,
            "is_admin": is_admin,
            "can_drag": can_drag,
            "departments": departments,
            "users": users,
            "projects": projects,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "today": date.today().isoformat(),
            "csrf_token": csrf,
        },
    )


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("/")
async def create_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_id: str = Form(""),
    due_date: str = Form(""),
    next_url: str = Form(""),
    csrf_token: str = Form(...),
    evidences: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    task = Task(
        id=uuid.uuid4(),
        title=title.strip(),
        description=description.strip() or None,
        priority=priority if priority in TASK_PRIORITIES else "medium",
        department_id=department_id if department_id else None,
        assigned_to=assigned_to if assigned_to else None,
        project_id=project_id if project_id else None,
        created_by=current_user.id,
        due_date=date.fromisoformat(due_date) if due_date else None,
    )
    db.add(task)
    db.flush()

    for f in evidences:
        if not f.filename:
            continue
        content = await f.read()
        if len(content) > MAX_EVIDENCE_BYTES:
            continue
        safe = _safe_filename(f.filename)
        key = f"tasks/{task.id}/{uuid.uuid4()}_{safe}"
        storage.upload_evidence(key, content, f.content_type or "application/octet-stream")
        db.add(TaskEvidence(
            id=uuid.uuid4(),
            task_id=task.id,
            uploaded_by=current_user.id,
            filename=f.filename,
            file_key=key,
            content_type=f.content_type or "application/octet-stream",
            file_size=len(content),
        ))

    db.commit()
    if task.assigned_to and str(task.assigned_to) != str(current_user.id):
        realtime.notify_user(task.assigned_to)
    audit.log_action(
        "task_create", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"priority={task.priority} evidences={len([f for f in evidences if f.filename])}",
    )
    redirect = next_url if next_url and next_url.startswith("/") else "/tasks/"
    return RedirectResponse(redirect, status_code=302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{task_id}", response_class=HTMLResponse)
def task_detail(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    task = (
        db.query(Task)
        .options(
            joinedload(Task.assignee),
            joinedload(Task.created_by_user),
            joinedload(Task.department),
            joinedload(Task.project),
            joinedload(Task.comments).joinedload(TaskComment.user),
            joinedload(Task.evidences).joinedload(TaskEvidence.uploader),
        )
        .filter(Task.id == task_id)
        .first()
    )
    if not task:
        raise HTTPException(404)

    # Verify visibility
    visible = _tasks_query(current_user, db).filter(Task.id == task_id).first()
    if not visible:
        raise HTTPException(403)

    users = db.query(User).filter(User.is_active == True).order_by(User.email).all()
    departments = db.query(Department).order_by(Department.name).all()

    return templates.TemplateResponse(
        request,
        "tasks/detail.html",
        {
            "current_user": current_user,
            "task": task,
            "users": users,
            "departments": departments,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "can_edit": _can_edit_task(current_user, task),
            "can_update_status": _can_update_status(current_user, task),
            "today": date.today().isoformat(),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


# ── Evidences ─────────────────────────────────────────────────────────────────

@router.post("/{task_id}/evidences")
async def upload_evidences(
    task_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    if not _tasks_query(current_user, db).filter(Task.id == task_id).first():
        raise HTTPException(403)

    errors = []
    for f in files:
        if not f.filename:
            continue
        content = await f.read()
        if len(content) > MAX_EVIDENCE_BYTES:
            errors.append(f"{f.filename}: excede 50 MB")
            continue
        safe = _safe_filename(f.filename)
        key = f"tasks/{task_id}/{uuid.uuid4()}_{safe}"
        storage.upload_evidence(key, content, f.content_type or "application/octet-stream")
        db.add(TaskEvidence(
            id=uuid.uuid4(),
            task_id=task.id,
            uploaded_by=current_user.id,
            filename=f.filename,
            file_key=key,
            content_type=f.content_type or "application/octet-stream",
            file_size=len(content),
        ))
    db.commit()
    uploaded = [f.filename for f in files if f.filename]
    task_obj = db.query(Task).filter(Task.id == task_id).first()
    audit.log_action(
        "evidence_upload", user=current_user, request=request,
        resource_type="task", resource_id=task_id,
        resource_name=task_obj.title if task_obj else task_id,
        details=f"files={len(uploaded)} names={','.join(uploaded[:5])}",
    )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.post("/{task_id}/evidences/{evidence_id}/delete")
def delete_evidence(
    task_id: str,
    evidence_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    ev = db.query(TaskEvidence).filter(
        TaskEvidence.id == evidence_id,
        TaskEvidence.task_id == task_id,
    ).first()
    if not ev:
        raise HTTPException(404)

    task = db.query(Task).filter(Task.id == task_id).first()
    is_uploader = str(ev.uploaded_by) == str(current_user.id)
    if not is_uploader and not _can_edit_task(current_user, task):
        raise HTTPException(403)

    filename = ev.filename
    try:
        storage.delete_evidence(ev.file_key)
    except Exception:
        pass
    db.delete(ev)
    db.commit()
    audit.log_action(
        "evidence_delete", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=filename,
    )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.get("/{task_id}/evidences/{evidence_id}/download")
def download_evidence(
    task_id: str,
    evidence_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not _tasks_query(current_user, db).filter(Task.id == task_id).first():
        raise HTTPException(403)

    ev = db.query(TaskEvidence).filter(
        TaskEvidence.id == evidence_id,
        TaskEvidence.task_id == task_id,
    ).first()
    if not ev:
        raise HTTPException(404)

    url = storage.get_evidence_url(ev.file_key, ev.filename)
    from fastapi.responses import RedirectResponse as RR
    return RR(url, status_code=302)


# ── Update status (HTMX) ──────────────────────────────────────────────────────

@router.post("/{task_id}/status", response_class=HTMLResponse)
def update_status(
    task_id: str,
    request: Request,
    status: str = Form(...),
    mode: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_update_status(current_user, task):
        raise HTTPException(403)
    if status not in TASK_STATUSES:
        raise HTTPException(400)
    prev = task.status
    task.status = status
    db.commit()
    # Notify the other party (assignee/creator) that the status changed.
    for uid in {task.assigned_to, task.created_by}:
        if uid and str(uid) != str(current_user.id):
            realtime.notify_user(uid)
    audit.log_action(
        "task_status_change", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"{prev} → {status}",
    )
    # Kanban drag-and-drop: no redirect, the card already moved client-side.
    if mode == "kanban":
        return HTMLResponse(status_code=204)
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


# ── Assign (HTMX) ─────────────────────────────────────────────────────────────

@router.post("/{task_id}/assign", response_class=HTMLResponse)
def assign_task(
    task_id: str,
    request: Request,
    assigned_to: str = Form(""),
    department_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task):
        raise HTTPException(403)
    task.assigned_to = assigned_to if assigned_to else None
    task.department_id = department_id if department_id else task.department_id
    db.commit()
    if task.assigned_to and str(task.assigned_to) != str(current_user.id):
        realtime.notify_user(task.assigned_to)
    assignee = db.query(User).filter(User.id == assigned_to).first() if assigned_to else None
    audit.log_action(
        "task_assign", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"assigned_to={assignee.email if assignee else 'none'}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{task_id}", response_class=HTMLResponse)
def delete_task(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task):
        raise HTTPException(403)
    title = task.title
    db.delete(task)
    db.commit()
    audit.log_action(
        "task_delete", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=title,
    )
    return HTMLResponse(headers={"HX-Redirect": "/tasks/"})


# ── Comments (HTMX append) ───────────────────────────────────────────────────

@router.post("/{task_id}/comments", response_class=HTMLResponse)
def add_comment(
    task_id: str,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    # Anyone who can see the task can comment
    visible = _tasks_query(current_user, db).filter(Task.id == task_id).first()
    if not visible:
        raise HTTPException(403)

    comment = TaskComment(
        id=uuid.uuid4(),
        task_id=task.id,
        user_id=current_user.id,
        content=content.strip(),
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    comment.user = current_user  # for template rendering

    return templates.TemplateResponse(
        request,
        "tasks/_comment.html",
        {"comment": comment, "current_user": current_user},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Recurring task templates (predefinidas por el encargado de cada área)
# ══════════════════════════════════════════════════════════════════════════════

recurring_router = APIRouter(prefix="/tasks/recurring", tags=["recurring-tasks"])


def _is_manager(user: User) -> bool:
    return user.role in (ROLE_ADMIN, ROLE_SUPERADMIN)


def _manager_scope(user: User, db: Session) -> dict | None:
    """Alcance de gestión de un encargado.

    - superadmin → None (sin restricción, todo).
    - admin CON zona(s) (gerente regional) → sólo las sucursales de sus zonas
      (`branch_ids`); no todo el departamento.
    - admin SIN zona (jefe de departamento) → todo su departamento (`dept_ids`).

    Devuelve un dict con `dept_ids` y `branch_ids` (uno de los dos vacío según el
    caso), o None para superadmin.
    """
    if user.role == ROLE_SUPERADMIN:
        return None

    zone_ids = [uz.zone_id for uz in user.user_zones]
    if zone_ids:
        branch_ids = {
            row[0] for row in db.query(Branch.id).filter(Branch.zone_id.in_(zone_ids)).all()
        }
        return {"dept_ids": set(), "branch_ids": branch_ids}

    return {"dept_ids": {user.department_id} if user.department_id else set(), "branch_ids": set()}


def _branch_user_ids(branch_ids: set, db: Session) -> set:
    """IDs de usuarios que pertenecen a las sucursales dadas."""
    if not branch_ids:
        return set()
    return {row[0] for row in db.query(User.id).filter(User.branch_id.in_(branch_ids)).all()}


def _assignable_department_ids(user: User, db: Session) -> set | None:
    """Departamentos que el encargado puede fijar en una plantilla.

    None = sin restricción (superadmin). Para gerentes de zona son los
    departamentos representados en sus sucursales (incluye departamentos-paraguas
    con branch_id nulo, derivados de los usuarios de esas sucursales)."""
    scope = _manager_scope(user, db)
    if scope is None:
        return None
    dept_ids = set(scope["dept_ids"])
    if scope["branch_ids"]:
        dept_ids |= {
            row[0] for row in db.query(Department.id)
            .filter(Department.branch_id.in_(scope["branch_ids"])).all()
        }
        dept_ids |= {
            row[0] for row in db.query(User.department_id)
            .filter(User.branch_id.in_(scope["branch_ids"]), User.department_id.isnot(None))
            .distinct().all()
        }
    return dept_ids


def _recurring_query(user: User, db: Session):
    q = db.query(RecurringTask).options(
        joinedload(RecurringTask.assignee),
        joinedload(RecurringTask.department),
        joinedload(RecurringTask.project),
        joinedload(RecurringTask.created_by_user),
    )
    scope = _manager_scope(user, db)
    if scope is None:
        return q  # superadmin ve todo
    conditions = [RecurringTask.created_by == user.id]
    if scope["dept_ids"]:
        conditions.append(RecurringTask.department_id.in_(scope["dept_ids"]))
    branch_user_ids = _branch_user_ids(scope["branch_ids"], db)
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
    if rt.assigned_to and rt.assigned_to in _branch_user_ids(scope["branch_ids"], db):
        return True
    return False


def _assignable_users_query(user: User, db: Session):
    """Usuarios activos que el encargado puede asignar, según su alcance."""
    q = db.query(User).filter(User.is_active == True)
    scope = _manager_scope(user, db)
    if scope is None:
        return q.order_by(User.email)
    conds = []
    if scope["dept_ids"]:
        conds.append(User.department_id.in_(scope["dept_ids"]))
    if scope["branch_ids"]:
        conds.append(User.branch_id.in_(scope["branch_ids"]))
    if not conds:
        return q.filter(false())
    return q.filter(or_(*conds)).order_by(User.email)


def _validate_target_scope(user: User, db: Session, department_id: str, assigned_to: str) -> None:
    """Rechaza departamento/asignado fuera del alcance del encargado.

    superadmin no tiene restricción. Para el resto, la plantilla debe apuntar a
    algo dentro de su alcance (su departamento o las sucursales de su zona)."""
    if user.role == ROLE_SUPERADMIN:
        return
    if not department_id and not assigned_to:
        raise HTTPException(400, "Elige un departamento o una persona dentro de tu alcance")
    allowed_depts = {str(d) for d in (_assignable_department_ids(user, db) or set())}
    if department_id and department_id not in allowed_depts:
        raise HTTPException(403, "Departamento fuera de tu alcance")
    if assigned_to:
        allowed_users = {str(u.id) for u in _assignable_users_query(user, db).all()}
        if assigned_to not in allowed_users:
            raise HTTPException(403, "Usuario fuera de tu alcance")


def _parse_recurrence(frequency: str, day_of_week: str, day_of_month: str):
    """Normaliza y valida la frecuencia. Devuelve (frequency, dow, dom)."""
    if frequency not in RECURRENCE_FREQUENCIES:
        raise HTTPException(400, "Frecuencia inválida")
    dow = dom = None
    if frequency == FREQ_WEEKLY:
        try:
            dow = int(day_of_week)
        except (TypeError, ValueError):
            raise HTTPException(400, "Día de la semana inválido")
        if not 0 <= dow <= 6:
            raise HTTPException(400, "Día de la semana fuera de rango")
    elif frequency == FREQ_MONTHLY:
        try:
            dom = int(day_of_month)
        except (TypeError, ValueError):
            raise HTTPException(400, "Día del mes inválido")
        if not 1 <= dom <= 31:
            raise HTTPException(400, "Día del mes fuera de rango")
    return frequency, dow, dom


@recurring_router.get("/", response_class=HTMLResponse)
def list_recurring(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    if not _is_manager(current_user):
        raise HTTPException(403)

    items = _recurring_query(current_user, db).order_by(RecurringTask.created_at.desc()).all()

    # Alcance: superadmin todo; jefe de depto → su departamento; gerente de zona
    # → las sucursales de sus zonas.
    dept_ids = _assignable_department_ids(current_user, db)
    dept_q = db.query(Department).order_by(Department.name)
    proj_q = db.query(Project).order_by(Project.name)
    if dept_ids is not None:
        scoped = dept_ids or {None}  # evita IN () vacío
        dept_q = dept_q.filter(Department.id.in_(scoped))
        proj_q = proj_q.filter(Project.department_id.in_(scoped))

    departments = dept_q.all()
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
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not _is_manager(current_user):
        raise HTTPException(403)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    _validate_target_scope(current_user, db, department_id, assigned_to)
    freq, dow, dom = _parse_recurrence(frequency, day_of_week, day_of_month)

    rt = RecurringTask(
        id=uuid.uuid4(),
        title=title.strip(),
        description=description.strip() or None,
        priority=priority if priority in TASK_PRIORITIES else "medium",
        department_id=department_id if department_id else None,
        assigned_to=assigned_to if assigned_to else None,
        project_id=project_id if project_id else None,
        created_by=current_user.id,
        frequency=freq,
        day_of_week=dow,
        day_of_month=dom,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
    )
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
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    rt = db.query(RecurringTask).filter(RecurringTask.id == rt_id).first()
    if not rt:
        raise HTTPException(404)
    if not _can_manage_recurring(current_user, rt, db):
        raise HTTPException(403)

    _validate_target_scope(current_user, db, department_id, assigned_to)
    freq, dow, dom = _parse_recurrence(frequency, day_of_week, day_of_month)

    rt.title = title.strip()
    rt.description = description.strip() or None
    rt.priority = priority if priority in TASK_PRIORITIES else "medium"
    rt.department_id = department_id if department_id else None
    rt.assigned_to = assigned_to if assigned_to else None
    rt.project_id = project_id if project_id else None
    rt.frequency = freq
    rt.day_of_week = dow
    rt.day_of_month = dom
    rt.start_date = date.fromisoformat(start_date) if start_date else None
    rt.end_date = date.fromisoformat(end_date) if end_date else None
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
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
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
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
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
