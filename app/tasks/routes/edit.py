"""Cambios a una tarea: estado, archivar, editar, asignar, etiquetas, proyectos, dependencias y eliminar."""

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.database import get_db
from app import audit
from app.messaging import realtime
from app.models import Task, TaskStatusHistory, TaskTagAssignment, User, TASK_STATUSES, TASK_DONE
from app.tasks import service as task_service
from app.tasks.service import _can_edit_task, _effective_department_id, _validate_target_scope
from app.tasks.helpers import (
    _can_approve_task,
    _can_update_status,
    _followers,
    _parse_date,
    _validated_tag_ids,
)

router = APIRouter()


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
    if not task or not _can_update_status(current_user, task, db):
        raise HTTPException(403)
    if status not in TASK_STATUSES:
        raise HTTPException(400)
    prev = task.status
    is_approval = status == TASK_DONE and prev != TASK_DONE
    if is_approval and not _can_approve_task(current_user, task):
        raise HTTPException(403, "Solo quien asignó la tarea o un superadmin puede aprobarla")
    task.status = status
    if status != prev:
        task.completed_at = datetime.utcnow() if status == TASK_DONE else None
        db.add(TaskStatusHistory(
            id=uuid.uuid4(),
            task_id=task.id,
            from_status=prev,
            to_status=status,
            changed_by=current_user.id,
        ))
    db.commit()
    # Notify the other party (assignee/creator) that the status changed.
    actor = current_user.name or current_user.email
    push_title = f"{actor} aprobó tu tarea" if is_approval else f"{actor} cambió el estado"
    for uid in _followers(task):
        if uid and str(uid) != str(current_user.id):
            realtime.notify_user(
                uid, title=push_title, body=task.title, url=f"/tasks/{task_id}",
                kind="task_approved" if is_approval else "task_status",
            )
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_status_change", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"{prev} → {status}",
    )
    if is_approval:
        audit.log_action(
            "task_approve", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
        )
    # Kanban drag-and-drop: no redirect, the card already moved client-side.
    if mode == "kanban":
        return HTMLResponse(status_code=204)
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


@router.post("/{task_id}/archive")
def archive_task(
    task_id: str,
    request: Request,
    next_url: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_update_status(current_user, task, db):
        raise HTTPException(403)
    if task.status != TASK_DONE:
        raise HTTPException(400, "Solo se pueden archivar tareas Listo")
    task.archived_at = datetime.utcnow()
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_archive", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
    )
    redirect = next_url if next_url and next_url.startswith("/") else "/tasks/"
    return RedirectResponse(redirect, status_code=302)


@router.post("/{task_id}/unarchive")
def unarchive_task(
    task_id: str,
    request: Request,
    next_url: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_update_status(current_user, task, db):
        raise HTTPException(403)
    task.archived_at = None
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_unarchive", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
    )
    redirect = next_url if next_url and next_url.startswith("/") else "/tasks/archived"
    return RedirectResponse(redirect, status_code=302)


@router.post("/{task_id}/edit", response_class=HTMLResponse)
def edit_task(
    task_id: str,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    due_date: str = Form(""),
    # El campo de inicio solo se pinta en tareas de proyectos con Gantt y va con
    # has_start=1; sin él no se toca el inicio guardado (un campo vacío llega
    # como None, así que no basta con revisar start_date).
    start_date: str = Form(""),
    has_start: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    title = title.strip()
    if not title:
        raise HTTPException(422, "El título es obligatorio")
    try:
        new_due = date.fromisoformat(due_date) if due_date else None
    except ValueError:
        raise HTTPException(422, "Fecha límite inválida")
    new_start = task.start_date
    if has_start and task_service.has_gantt(task):
        new_start = _parse_date(start_date, "Fecha de inicio inválida")
    task_service.check_start_due(new_start, new_due)

    changed = []
    if title[:255] != task.title:
        changed.append("title")
    if (description.strip() or None) != task.description:
        changed.append("description")
    if new_due != task.due_date:
        changed.append("due_date")
    if new_start != task.start_date:
        changed.append("start_date")
    task.start_date = new_start
    task.title = title[:255]
    task.description = description.strip() or None
    task.due_date = new_due
    db.commit()
    if changed:
        actor = current_user.name or current_user.email
        for uid in _followers(task):
            if uid and str(uid) != str(current_user.id):
                realtime.notify_user(
                    uid, title=f"{actor} editó la tarea", body=task.title,
                    url=f"/tasks/{task_id}", kind="task_updated",
                )
        realtime.notify_tasks()
        realtime.notify_task(task_id)
        audit.log_action(
            "task_update", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
            details=f"fields={','.join(changed)}",
        )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


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
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    _validate_target_scope(current_user, db, department_id, assigned_to, required=False)
    old_dept_id = task.department_id
    task.assigned_to = assigned_to if assigned_to else None
    task.department_id = department_id if department_id else (
        task.department_id or _effective_department_id(None, assigned_to, db)
    )
    if department_id and str(task.department_id) != str(old_dept_id):
        # Las etiquetas son por departamento: si cambia, las del depto anterior dejan de aplicar.
        db.query(TaskTagAssignment).filter(TaskTagAssignment.task_id == task_id).delete()
    db.commit()
    if task.assigned_to and str(task.assigned_to) != str(current_user.id):
        actor = current_user.name or current_user.email
        realtime.notify_user(
            task.assigned_to,
            title=f"{actor} te asignó una tarea",
            body=task.title,
            url=f"/tasks/{task_id}",
            kind="task_assigned",
        )
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    assignee = db.query(User).filter(User.id == assigned_to).first() if assigned_to else None
    audit.log_action(
        "task_assign", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"assigned_to={assignee.email if assignee else 'none'}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


@router.post("/{task_id}/tags", response_class=HTMLResponse)
def set_task_tags(
    task_id: str,
    request: Request,
    tag_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)

    valid_ids = _validated_tag_ids(tag_ids, str(task.department_id) if task.department_id else None, db)
    db.query(TaskTagAssignment).filter(TaskTagAssignment.task_id == task_id).delete()
    for tid in valid_ids:
        db.add(TaskTagAssignment(task_id=task_id, tag_id=tid))
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_tags_update", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"tags={len(valid_ids)}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


@router.post("/{task_id}/projects", response_class=HTMLResponse)
def set_task_projects(
    task_id: str,
    request: Request,
    project_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Proyectos de la tarea: solo se eligen desde aquí (las recurrentes llevan uno)."""
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).options(joinedload(Task.projects)).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)

    task_service.set_task_projects(task, project_ids, current_user, db)
    names = [p.name for p in task.projects]
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_projects_update", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"projects={len(names)} names={','.join(names[:5])}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


@router.post("/{task_id}/dependencies", response_class=HTMLResponse)
def add_task_dependency(
    task_id: str,
    request: Request,
    depends_on_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """La tarea pasa a esperar a `depends_on_id` (mismo proyecto con Gantt, sin ciclos)."""
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).options(joinedload(Task.projects)).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    other = task_service.add_dependency(task, depends_on_id, db)
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    realtime.notify_task(other.id)
    audit.log_action(
        "task_dependency_add", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"depends_on={other.id} {other.title[:80]}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


@router.post("/{task_id}/dependencies/{depends_on_id}/delete", response_class=HTMLResponse)
def remove_task_dependency(
    task_id: str,
    depends_on_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    removed = task_service.remove_dependency(task, depends_on_id)
    if removed:
        db.commit()
        realtime.notify_tasks()
        realtime.notify_task(task_id)
        realtime.notify_task(removed.id)
        audit.log_action(
            "task_dependency_remove", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
            details=f"depends_on={removed.id} {removed.title[:80]}",
        )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


@router.delete("/{task_id}", response_class=HTMLResponse)
def delete_task(
    task_id: str,
    request: Request,
    back: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    title = task.title
    db.delete(task)
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_delete", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=title,
    )
    # Vuelve a la lista de origen (tab/filtros); solo rutas propias de /tasks/.
    redirect = back if back.startswith("/tasks/") and "//" not in back else "/tasks/"
    return HTMLResponse(headers={"HX-Redirect": redirect})
