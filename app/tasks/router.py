import re
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app import audit, storage
from app.models import (
    Department, Project, Task, TaskComment, TaskEvidence, User,
    ROLE_SUPERADMIN, ROLE_ADMIN,
    TASK_STATUSES, TASK_PRIORITIES,
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
    audit.log_action(
        "task_status_change", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"{prev} → {status}",
    )
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
