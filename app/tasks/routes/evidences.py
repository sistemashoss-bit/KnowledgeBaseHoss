"""Evidencias de una tarea: subir, eliminar y descargar."""

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app import audit, storage, images
from app.messaging import realtime
from app.models import Task, TaskEvidence
from app.tasks.service import _can_edit_task, _can_view_task, _tasks_query
from app.tasks.helpers import (
    _followers,
    _safe_filename,
)

router = APIRouter()


@router.post("/{task_id}/evidences")
def upload_evidences(
    task_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    if not _tasks_query(current_user, db).filter(Task.id == task_id).first():
        raise HTTPException(403)

    storage.check_upload_sizes(*files)

    for f in files:
        if not f.filename:
            continue
        content, filename, ctype = images.optimize_image(
            f.file.read(), f.filename, f.content_type or "application/octet-stream",
        )
        safe = _safe_filename(filename)
        key = f"tasks/{task_id}/{uuid.uuid4()}_{safe}"
        storage.upload_evidence(key, content, ctype)
        db.add(TaskEvidence(
            id=uuid.uuid4(),
            task_id=task.id,
            uploaded_by=current_user.id,
            filename=filename,
            file_key=key,
            content_type=ctype,
            file_size=len(content),
        ))
    db.commit()
    uploaded = [f.filename for f in files if f.filename]
    if uploaded:
        realtime.notify_task(task_id)
    task_obj = db.query(Task).filter(Task.id == task_id).first()
    audit.log_action(
        "evidence_upload", user=current_user, request=request,
        resource_type="task", resource_id=task_id,
        resource_name=task_obj.title if task_obj else task_id,
        details=f"files={len(uploaded)} names={','.join(uploaded[:5])}",
    )
    # Notifica al asignado y al creador (menos a quien sube), solo si hubo archivos.
    if uploaded:
        actor = current_user.name or current_user.email
        for uid in _followers(task):
            if uid and str(uid) != str(current_user.id):
                realtime.notify_user(
                    uid,
                    title=f"{actor} subió evidencia",
                    body=task.title,
                    url=f"/tasks/{task_id}",
                    kind="task_evidence",
                )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.post("/{task_id}/evidences/{evidence_id}/delete")
def delete_evidence(
    task_id: str,
    evidence_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    ev = db.query(TaskEvidence).filter(
        TaskEvidence.id == evidence_id,
        TaskEvidence.task_id == task_id,
    ).first()
    if not ev:
        raise HTTPException(404)

    task = db.query(Task).filter(Task.id == task_id).first()
    is_uploader = str(ev.uploaded_by) == str(current_user.id)
    if not is_uploader and not _can_edit_task(current_user, task, db):
        raise HTTPException(403)

    filename = ev.filename
    try:
        storage.delete_evidence(ev.file_key)
    except Exception:
        pass
    db.delete(ev)
    db.commit()
    realtime.notify_task(task_id)
    audit.log_action(
        "evidence_delete", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=filename,
    )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.get("/{task_id}/evidences/{evidence_id}/download")
def download_evidence(
    task_id: str,
    evidence_id: str,
    inline: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not _can_view_task(current_user, task_id, db):
        raise HTTPException(403)

    ev = db.query(TaskEvidence).filter(
        TaskEvidence.id == evidence_id,
        TaskEvidence.task_id == task_id,
    ).first()
    if not ev:
        raise HTTPException(404)

    url = storage.get_evidence_url(ev.file_key, ev.filename, inline=inline)
    from fastapi.responses import RedirectResponse as RR
    return RR(url, status_code=302)
