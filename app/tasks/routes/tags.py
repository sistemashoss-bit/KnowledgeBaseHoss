"""Catálogo de etiquetas de tareas (/tasks/tags)."""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app import audit
from app.models import Department, TaskTag, ROLE_SUPERADMIN, TASK_TAG_COLORS
from app.templating import templates


tags_router = APIRouter(prefix="/tasks/tags", tags=["task-tags"])


@tags_router.get("/", response_class=HTMLResponse)
def list_tags(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    departments = db.query(Department).order_by(Department.name).all()
    tags = db.query(TaskTag).order_by(TaskTag.name).all()
    return templates.TemplateResponse(
        request, "tasks/tags.html",
        {
            "current_user": current_user,
            "departments": departments,
            "tags": tags,
            "colors": TASK_TAG_COLORS,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@tags_router.post("/")
def create_tag(
    request: Request,
    name: str = Form(...),
    color: str = Form("gray"),
    department_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    if not db.query(Department).filter(Department.id == department_id).first():
        raise HTTPException(404, "Department not found")

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre no puede estar vacío")
    color = color if color in TASK_TAG_COLORS else "gray"

    if db.query(TaskTag).filter(TaskTag.department_id == department_id, TaskTag.name == name).first():
        raise HTTPException(400, "Ya existe una etiqueta con ese nombre en este departamento")

    tag = TaskTag(
        id=uuid.uuid4(), name=name, color=color,
        department_id=department_id, created_by=current_user.id,
    )
    db.add(tag)
    db.commit()
    audit.log_action(
        "task_tag_create", user=current_user, request=request,
        resource_type="task_tag", resource_id=tag.id, resource_name=tag.name,
    )
    return RedirectResponse("/tasks/tags/", status_code=302)


@tags_router.post("/{tag_id}/edit")
def edit_tag(
    tag_id: str,
    request: Request,
    name: str = Form(...),
    color: str = Form("gray"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    tag = db.query(TaskTag).filter(TaskTag.id == tag_id).first()
    if not tag:
        raise HTTPException(404)

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre no puede estar vacío")
    dup = db.query(TaskTag).filter(
        TaskTag.department_id == tag.department_id, TaskTag.name == name, TaskTag.id != tag.id,
    ).first()
    if dup:
        raise HTTPException(400, "Ya existe una etiqueta con ese nombre en este departamento")

    tag.name = name
    tag.color = color if color in TASK_TAG_COLORS else "gray"
    db.commit()
    audit.log_action(
        "task_tag_update", user=current_user, request=request,
        resource_type="task_tag", resource_id=tag.id, resource_name=tag.name,
    )
    return RedirectResponse("/tasks/tags/", status_code=302)


@tags_router.post("/{tag_id}/delete")
def delete_tag(
    tag_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    tag = db.query(TaskTag).filter(TaskTag.id == tag_id).first()
    if not tag:
        raise HTTPException(404)

    name = tag.name
    db.delete(tag)
    db.commit()
    audit.log_action(
        "task_tag_delete", user=current_user, request=request,
        resource_type="task_tag", resource_id=tag_id, resource_name=name,
    )
    return RedirectResponse("/tasks/tags/", status_code=302)
