import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app.models import (
    Branch, Department, Project, Task, User, UserZone, Zone,
    ROLE_SUPERADMIN, ROLE_ADMIN,
    PROJECT_STATUSES, TASK_STATUSES, TASK_PRIORITIES,
)
from app.templating import templates

router = APIRouter(prefix="/projects", tags=["projects"])


# ── Visibility ────────────────────────────────────────────────────────────────

def _projects_query(user: User, db: Session):
    q = db.query(Project).options(
        joinedload(Project.department),
        joinedload(Project.branch),
        joinedload(Project.zone),
        joinedload(Project.created_by_user),
        joinedload(Project.tasks),
    )
    if user.role == ROLE_SUPERADMIN:
        return q

    conditions = [Project.created_by == user.id]

    if user.department_id:
        conditions.append(Project.department_id == user.department_id)
    if user.branch_id:
        conditions.append(Project.branch_id == user.branch_id)

    zone_ids = [uz.zone_id for uz in db.query(UserZone).filter(UserZone.user_id == user.id).all()]
    if zone_ids:
        conditions.append(Project.zone_id.in_(zone_ids))

    return q.filter(or_(*conditions))


def _can_edit_project(user: User, project: Project) -> bool:
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(project.created_by) == str(user.id):
        return True
    if user.role == ROLE_ADMIN and project.department_id and str(project.department_id) == str(user.department_id):
        return True
    return False


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
    name: str = Form(...),
    description: str = Form(""),
    status: str = Form("draft"),
    department_id: str = Form(""),
    branch_id: str = Form(""),
    zone_id: str = Form(""),
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
    return RedirectResponse(f"/projects/{project.id}", status_code=302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{project_id}", response_class=HTMLResponse)
def project_detail(
    project_id: str,
    request: Request,
    task_status: str = "",
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
        .filter(Task.project_id == project_id)
    )
    if task_status and task_status in TASK_STATUSES:
        tasks_q = tasks_q.filter(Task.status == task_status)
    tasks = tasks_q.order_by(Task.created_at.desc()).all()

    # Stats over ALL tasks in project (ignore task_status filter for counts)
    all_tasks = db.query(Task).filter(Task.project_id == project_id).all()
    stats = {s: 0 for s in TASK_STATUSES}
    for t in all_tasks:
        stats[t.status] = stats.get(t.status, 0) + 1
    total = len(all_tasks)
    done_pct = round((stats.get("done", 0) / total) * 100) if total else 0

    departments = db.query(Department).order_by(Department.name).all()
    users = db.query(User).filter(User.is_active == True).order_by(User.email).all()

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
            "users": users,
            "task_statuses": TASK_STATUSES,
            "task_priorities": TASK_PRIORITIES,
            "project_statuses": PROJECT_STATUSES,
            "filter_task_status": task_status,
            "can_edit": _can_edit_project(current_user, project),
            "today": date.today().isoformat(),
        },
    )


# ── Update status ─────────────────────────────────────────────────────────────

@router.post("/{project_id}/status", response_class=HTMLResponse)
def update_project_status(
    project_id: str,
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
    project.status = status
    db.commit()
    return HTMLResponse(headers={"HX-Redirect": f"/projects/{project_id}"})


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{project_id}", response_class=HTMLResponse)
def delete_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project or not _can_edit_project(current_user, project):
        raise HTTPException(403)
    # Detach tasks rather than cascade-delete them
    db.query(Task).filter(Task.project_id == project_id).update({"project_id": None})
    db.delete(project)
    db.commit()
    return HTMLResponse(headers={"HX-Redirect": "/projects/"})
