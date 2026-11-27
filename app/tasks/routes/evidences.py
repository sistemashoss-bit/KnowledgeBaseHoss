"""Evidencias de una tarea: subir, eliminar y descargar."""

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app import attachments, audit, storage
from app.messaging import realtime
from app.models import Task, TaskEvidence
from app.tasks.service import _can_edit_task, _can_view_task, _tasks_query
from app.tasks.helpers import _followers

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
    task = _task_or_404(task_id, db)
    if not _tasks_query(current_user, db).filter(Task.id == task.id).first():
        raise HTTPException(403)

    storage.check_upload_sizes(*files)
    stored = attachments.store_uploads(f"tasks/{task.id}", files)
    for f in stored:
        db.add(TaskEvidence(id=uuid.uuid4(), task_id=task.id, uploaded_by=current_user.id, **f))
    db.commit()
    uploaded = [f["filename"] for f in stored]
    if uploaded:
        realtime.notify_task(task.id)
    audit.log_action(
        "evidence_upload", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"files={len(uploaded)} names={','.join(uploaded[:5])}",
    )
    # Notifica al asignado y al creador (menos a quien sube), solo si hubo archivos.
    if uploaded:
        actor = current_user.name or current_user.email
        ids = [uid for uid in _followers(task) if uid and str(uid) != str(current_user.id)]
        if ids:
            realtime.notify_users(
                ids, title=f"{actor} subió evidencia", body=task.title, url=f"/tasks/{task.id}", kind="task_evidence",
            )
    return RedirectResponse(f"/tasks/{task.id}", status_code=302)


def _task_or_404(task_id: str, db: Session) -> Task:
    try:
        task = db.get(Task, uuid.UUID(str(task_id)))
    except ValueError:
        task = None
    if not task:
        raise HTTPException(404)
    return task


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
    task = _task_or_404(task_id, db)
    ev = attachments.get_or_404(db, TaskEvidence, TaskEvidence.task_id, task.id, evidence_id)
    is_uploader = str(ev.uploaded_by) == str(current_user.id)
    if not is_uploader and not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    filename = attachments.remove(ev, db)
    db.commit()
    realtime.notify_task(task.id)
    audit.log_action(
        "evidence_delete", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=filename,
    )
    return RedirectResponse(f"/tasks/{task.id}", status_code=302)


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
    task = _task_or_404(task_id, db)
    if not _can_view_task(current_user, task.id, db):
        raise HTTPException(403)
    ev = attachments.get_or_404(db, TaskEvidence, TaskEvidence.task_id, task.id, evidence_id)
    return attachments.download(ev, inline)
