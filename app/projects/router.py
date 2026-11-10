import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload, selectinload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app import audit
from app.permissions import managed_department_ids, visible_projects_query as _projects_query
from app.models import (
    Branch, Department, Project, Task, User, Zone,
    ROLE_SUPERADMIN,
    PROJECT_STATUSES, TASK_STATUSES, TASK_PRIORITIES, TASK_DONE, TASK_IN_PROGRESS, TASK_REVIEW,
)
from app.tasks.service import is_blocked
from app.templating import templates

router = APIRouter(prefix="/projects", tags=["projects"])


# ── Visibility ────────────────────────────────────────────────────────────────

def _can_edit_project(user: User, project: Project) -> bool:
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(project.created_by) == str(user.id):
        return True
    if project.department_id and project.department_id in managed_department_ids(user):
        return True
    return False


# ── Gantt ─────────────────────────────────────────────────────────────────────

_GANTT_PROGRESS = {TASK_IN_PROGRESS: 50, TASK_REVIEW: 75, TASK_DONE: 100}
_STATUS_LABELS = {"pending": "Pendiente", "in_progress": "En progreso", "review": "Revisión", "done": "Listo"}


def _gantt_data(tasks: list[Task]) -> tuple[list[dict], list[Task]]:
    """Barras para Frappe Gantt y las tareas que no se pueden dibujar (sin
    vencimiento). Inicio = start_date o created_at; fin = due_date. Si el inicio
    queda después del fin (p. ej. se creó ya vencida) la barra es de un día."""
    ids = {t.id for t in tasks}
    bars, undated = [], []
    for t in tasks:
        if not t.due_date:
            undated.append(t)
            continue
        start = t.start_date or (t.created_at.date() if t.created_at else t.due_date)
        start = min(start, t.due_date)
        blocked = is_blocked(t)
        who = t.assignee.name or t.assignee.email if t.assignee else (t.department.name if t.department else "")
        bars.append({
            "id": str(t.id),
            "name": t.title,
            "start": start.isoformat(),
            "end": t.due_date.isoformat(),
            "progress": _GANTT_PROGRESS.get(t.status, 0),
            "dependencies": ",".join(str(d.id) for d in t.depends_on if d.id in ids),
            "custom_class": f"g-{t.status}" + ("-blocked" if blocked else ""),
            "status": _STATUS_LABELS.get(t.status, t.status),
            "who": who,
            "estimated": t.start_date is None,
        })
    bars.sort(key=lambda b: (b["start"], b["end"]))
    return bars, undated


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def list_projects(
    request: Request,
    status: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    q = _projects_query(current_user, db)
    if status and status in PROJECT_STATUSES:
        q = q.filter(Project.status == status)

    projects = q.order_by(Project.created_at.desc()).all()

    departments = db.query(Department).order_by(Department.name).all()
    branches = db.query(Branch).order_by(Branch.name).all()
    zones = db.query(Zone).order_by(Zone.name).all()

    csrf = generate_csrf_token(str(current_user.id))
    return templates.TemplateResponse(
        request,
        "projects/list.html",
        {
            "current_user": current_user,
            "projects": projects,
            "departments": departments,
            "branches": branches,
            "zones": zones,
            "statuses": PROJECT_STATUSES,
            "filter_status": status,
            "today": date.today().isoformat(),
            "csrf_token": csrf,
        },
    )


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("/")
def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    status: str = Form("draft"),
    department_id: str = Form(""),
    branch_id: str = Form(""),
    zone_id: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    project = Project(
        id=uuid.uuid4(),
        name=name.strip(),
        description=description.strip() or None,
        status=status if status in PROJECT_STATUSES else "draft",
        department_id=department_id or None,
        branch_id=branch_id or None,
        zone_id=zone_id or None,
        created_by=current_user.id,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
    )
    db.add(project)
    db.commit()
    audit.log_action(
        "project_create", user=current_user, request=request,
        resource_type="project", resource_id=project.id, resource_name=project.name,
        details=f"status={project.status}",
    )
    return RedirectResponse(f"/projects/{project.id}", status_code=302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}", response_class=HTMLResponse)
def project_detail(
    project_id: str,
    request: Request,
    task_status: str = "",
    view: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    project = (
        db.query(Project)
        .options(
            joinedload(Project.department),
            joinedload(Project.branch),
            joinedload(Project.zone),
            joinedload(Project.created_by_user),
        )
        .filter(Project.id == project_id)
        .first()
    )
    if not project:
        raise HTTPException(404)

    if not _projects_query(current_user, db).filter(Project.id == project_id).first():
        raise HTTPException(403)

    # Tasks belonging to this project
    tasks_q = (
        db.query(Task)
        .options(
            joinedload(Task.assignee),
            joinedload(Task.department),
        )
        .filter(Task.projects.any(Project.id == project_id))
    )
    if task_status and task_status in TASK_STATUSES:
        tasks_q = tasks_q.filter(Task.status == task_status)
    tasks = tasks_q.order_by(Task.created_at.desc()).all()

    # Stats over ALL tasks in project (ignore task_status filter for counts)
    all_tasks = db.query(Task).filter(Task.projects.any(Project.id == project_id)).all()
    stats = {s: 0 for s in TASK_STATUSES}
    for t in all_tasks:
        stats[t.status] = stats.get(t.status, 0) + 1
    total = len(all_tasks)
    done_pct = round((stats.get("done", 0) / total) * 100) if total else 0

    view = "gantt" if view == "gantt" and project.gantt_enabled else "list"
    gantt_tasks, gantt_undated = [], []
    if view == "gantt":
        gantt_source = (
            db.query(Task)
            .options(joinedload(Task.assignee), joinedload(Task.department), selectinload(Task.depends_on))
            .filter(Task.projects.any(Project.id == project_id))
            .all()
        )
        gantt_tasks, gantt_undated = _gantt_data(gantt_source)

    departments = db.query(Department).order_by(Department.name).all()
    users = db.query(User).filter(User.is_active == True).order_by(User.email).all()
    can_edit = _can_edit_project(current_user, project)
    branches = db.query(Branch).order_by(Branch.name).all() if can_edit else []
    zones = db.query(Zone).order_by(Zone.name).all() if can_edit else []

    return templates.TemplateResponse(
        request,
        "projects/detail.html",
        {
            "current_user": current_user,
            "project": project,
            "tasks": tasks,
            "stats": stats,
            "total": total,
            "done_pct": done_pct,
            "departments": departments,
            "branches": branches,
            "zones": zones,
            "users": users,
            "task_statuses": TASK_STATUSES,
            "task_priorities": TASK_PRIORITIES,
            "project_statuses": PROJECT_STATUSES,
            "filter_task_status": task_status,
            "view": view,
            "gantt_tasks": gantt_tasks,
            "gantt_undated": gantt_undated,
            "can_edit": can_edit,
            "today": date.today().isoformat(),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


# ── Edit ──────────────────────────────────────────────────────────────────────

@router.post("/{project_id}/edit")
def edit_project(
    project_id: str,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    department_id: str = Form(""),
    branch_id: str = Form(""),
    zone_id: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    gantt_enabled: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not _can_edit_project(current_user, project):
        raise HTTPException(403)
    if not name.strip():
        raise HTTPException(400, "El nombre es obligatorio")
    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError:
        raise HTTPException(400, "Fecha inválida")
    if start and end and end < start:
        raise HTTPException(400, "La fecha límite no puede ser anterior al inicio")

    project.name = name.strip()
    project.description = description.strip() or None
    project.department_id = department_id or None
    project.branch_id = branch_id or None
    project.zone_id = zone_id or None
    project.start_date = start
    project.end_date = end
    project.gantt_enabled = bool(gantt_enabled)
    db.commit()
    audit.log_action(
        "project_edit", user=current_user, request=request,
        resource_type="project", resource_id=project_id, resource_name=project.name,
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=302)


# ── Update status ─────────────────────────────────────────────────────────────

@router.post("/{project_id}/status", response_class=HTMLResponse)
def update_project_status(
    project_id: str,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not _can_edit_project(current_user, project):
        raise HTTPException(403)
    if status not in PROJECT_STATUSES:
        raise HTTPException(400)
    prev = project.status
    project.status = status
    db.commit()
    audit.log_action(
        "project_status_change", user=current_user, request=request,
        resource_type="project", resource_id=project_id, resource_name=project.name,
        details=f"{prev} → {status}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/projects/{project_id}"})


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{project_id}", response_class=HTMLResponse)
def delete_project(
    project_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not _can_edit_project(current_user, project):
        raise HTTPException(403)
    name = project.name
    db.delete(project)
    db.commit()
    audit.log_action(
        "project_delete", user=current_user, request=request,
        resource_type="project", resource_id=project_id, resource_name=name,
    )
    return HTMLResponse(headers={"HX-Redirect": "/projects/"})
