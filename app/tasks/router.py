import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app.models import (
    Department, Project, Task, TaskComment, User,
    ROLE_SUPERADMIN, ROLE_ADMIN,
    TASK_STATUSES, TASK_PRIORITIES,
)
from app.templating import templates

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
    status: str = "",
    mine: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    q = _tasks_query(current_user, db)

    if mine == "1":
        q = q.filter(Task.assigned_to == current_user.id)
    if status and status in TASK_STATUSES:
        q = q.filter(Task.status == status)

    tasks = q.order_by(Task.created_at.desc()).all()

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
            "tasks": tasks,
            "departments": departments,
            "users": users,
            "projects": projects,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "filter_status": status,
            "filter_mine": mine,
            "today": date.today().isoformat(),
            "csrf_token": csrf,
        },
    )


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("/")
def create_task(
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_id: str = Form(""),
    due_date: str = Form(""),
    next_url: str = Form(""),
    csrf_token: str = Form(...),
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
    db.commit()
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
        },
    )


# ── Update status (HTMX) ──────────────────────────────────────────────────────

@router.post("/{task_id}/status", response_class=HTMLResponse)
def update_status(
    task_id: str,
    request: Request,
    status: str = Form(...),
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
    task.status = status
    db.commit()
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


# ── Assign (HTMX) ─────────────────────────────────────────────────────────────

@router.post("/{task_id}/assign", response_class=HTMLResponse)
def assign_task(
    task_id: str,
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
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{task_id}", response_class=HTMLResponse)
def delete_task(
    task_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task):
        raise HTTPException(403)
    db.delete(task)
    db.commit()
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
