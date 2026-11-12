"""Seguimientos de una tarea (bloques con hora en el calendario del responsable)."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app import audit
from app.messaging import realtime
from app.models import Task, TaskComment, TaskFollowUp, User
from app.calendar.reminders import to_local
from app.tasks import service as task_service
from app.tasks.service import _can_manage_follow_ups, _parse_follow_up, _schedule_follow_up
from app.tasks.helpers import (
    _follow_up_view,
    _followers,
)

router = APIRouter()


def _follow_up_task(task_id: str, user: User, db: Session) -> Task:
    try:
        uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(404)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    if not _can_manage_follow_ups(user, task, db):
        raise HTTPException(403)
    return task


def _task_follow_up(task: Task, follow_up_id: str, db: Session) -> TaskFollowUp:
    try:
        uuid.UUID(follow_up_id)
    except ValueError:
        raise HTTPException(404)
    f = db.query(TaskFollowUp).filter(TaskFollowUp.id == follow_up_id, TaskFollowUp.task_id == task.id).first()
    if not f:
        raise HTTPException(404)
    return f


@router.post("/{task_id}/follow-ups")
def add_follow_up(
    task_id: str,
    request: Request,
    day: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    reminder_minutes: str = Form(""),
    note: str = Form(""),
    repeat: str = Form("false"),
    frequency: str = Form(""),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    repeat_until: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    result = task_service.create_follow_ups(
        db, current_user, task,
        day=day, start=start, end=end, reminder_minutes=reminder_minutes, note=note,
        repeat=repeat == "true", frequency=frequency, day_of_week=day_of_week,
        day_of_month=day_of_month, days_of_week=days_of_week, repeat_until=repeat_until,
    )
    db.commit()
    task_service.notify_follow_ups_created(task, current_user, result)

    start_at, end_at, days, series_id = result["start_at"], result["end_at"], result["days"], result["series_id"]
    audit.log_action(
        "task_follow_up_add", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"{to_local(start_at):%Y-%m-%d %H:%M}-{to_local(end_at):%H:%M}"
        + (f" x{len(days)} {frequency} hasta {days[-1].isoformat()}" if series_id else ""),
    )
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/edit")
def edit_follow_up(
    task_id: str,
    follow_up_id: str,
    day: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    reminder_minutes: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    start_at, end_at, reminder = _parse_follow_up(task, day, start, end, reminder_minutes, existing=f)
    _schedule_follow_up(f, start_at, end_at, reminder)
    f.note = note.strip()[:500] or None
    db.commit()
    realtime.notify_task(task.id)
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/done")
def done_follow_up(
    task_id: str,
    follow_up_id: str,
    request: Request,
    comment: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Marca el seguimiento como hecho y lo deja asentado en los comentarios."""
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    if f.done_at:
        return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)
    f.done_at = datetime.utcnow()
    f.done_by = current_user.id
    view = _follow_up_view(f)
    content = f"✔ Seguimiento del {view['label']} hecho."
    if comment.strip():
        content += f"\n{comment.strip()}"
    db.add(TaskComment(id=uuid.uuid4(), task_id=task.id, user_id=current_user.id, content=content))
    db.commit()

    actor = current_user.name or current_user.email
    for uid in _followers(task):
        if uid and str(uid) != str(current_user.id):
            realtime.notify_user(
                uid,
                title=f"{actor} dio seguimiento",
                body=f"{task.title} · {view['label']}",
                url=f"/tasks/{task.id}",
                kind="task_follow_up",
            )
    realtime.notify_task(task.id)
    audit.log_action(
        "task_follow_up_done", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=view["label"],
    )
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/undo")
def undo_follow_up(
    task_id: str,
    follow_up_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    f.done_at = f.done_by = None
    db.commit()
    realtime.notify_task(task.id)
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/delete")
def delete_follow_up(
    task_id: str,
    follow_up_id: str,
    request: Request,
    scope: str = Form("one"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Quita el seguimiento; con scope=following, también los siguientes de su
    serie que sigan pendientes (los hechos se conservan como historial)."""
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    label = _follow_up_view(f)["label"]
    if scope == "following" and f.series_id:
        removed = db.query(TaskFollowUp).filter(
            TaskFollowUp.task_id == task.id,
            TaskFollowUp.series_id == f.series_id,
            TaskFollowUp.start_at >= f.start_at,
            TaskFollowUp.done_at.is_(None),
        ).delete(synchronize_session=False)
        if f.done_at:
            db.delete(f)
            removed += 1
        label += f" y siguientes ({removed})"
    else:
        db.delete(f)
    db.commit()
    realtime.notify_task(task.id)
    audit.log_action(
        "task_follow_up_delete", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title, details=label,
    )
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)
