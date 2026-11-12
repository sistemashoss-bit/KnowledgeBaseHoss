"""Colaboración en una tarea: compartir (seguimiento), comentarios y sus reacciones."""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app import audit, reactions
from app.messaging import realtime, stickers
from app.models import Task, TaskComment, TaskCommentReaction, TaskShare, User
from app.tasks.service import _can_edit_task, _can_view_task
from app.templating import templates
from app.tasks.helpers import (
    _add_shares,
    _can_comment,
    _can_share,
    _followers,
    _notify_shared,
)

router = APIRouter()


@router.post("/{task_id}/shares")
def add_task_shares(
    task_id: str,
    request: Request,
    shared_with: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_share(current_user, task, db):
        raise HTTPException(403)
    added = _add_shares(task, shared_with, current_user, db)
    db.commit()
    if added:
        _notify_shared(task, added, current_user)
        realtime.notify_task(task_id)
        audit.log_action(
            "task_share", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
            details=f"users={len(added)}",
        )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.post("/{task_id}/shares/{user_id}/remove")
def remove_task_share(
    task_id: str,
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    try:
        user_id = str(uuid.UUID(user_id))
    except ValueError:
        raise HTTPException(404)
    # Quita quien puede compartir, o la propia persona deja de seguirla.
    is_self = user_id == str(current_user.id)
    if not (is_self or _can_share(current_user, task, db)):
        raise HTTPException(403)
    share = db.query(TaskShare).filter(TaskShare.task_id == task.id, TaskShare.user_id == user_id).first()
    if share:
        db.delete(share)
        db.commit()
        realtime.notify_task(task_id)
        audit.log_action(
            "task_unshare", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
            details=f"user={user_id}",
        )
    return RedirectResponse("/tasks/?tab=shared" if is_self else f"/tasks/{task_id}", status_code=302)


@router.post("/{task_id}/comments", response_class=HTMLResponse)
def add_comment(
    task_id: str,
    request: Request,
    content: str = Form(default=""),
    sticker: str = Form(default=""),
    parent_id: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    # Comenta quien participa por su alcance o a quien se la compartieron
    if not _can_comment(current_user, task_id, db):
        raise HTTPException(403)
    # Respuesta: siempre cuelga del comentario raíz (hilos de un solo nivel).
    parent = _task_comment(task_id, parent_id, db) if parent_id else None
    if parent and parent.parent_id:
        parent = db.get(TaskComment, parent.parent_id) or parent

    # Un sticker se guarda como la URL de su APNG (igual que en el chat).
    content = stickers.require_url(sticker) if sticker else content.strip()
    if not content:
        raise HTTPException(400, "El comentario no puede quedar vacío")
    comment = TaskComment(
        id=uuid.uuid4(),
        task_id=task.id,
        user_id=current_user.id,
        parent_id=parent.id if parent else None,
        content=content,
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    comment.user = current_user  # for template rendering

    # Notifica al asignado, al creador y a con quienes se compartió (menos a quien
    # comenta). En una respuesta, el autor del comentario raíz recibe "respondió a
    # tu comentario" en lugar del aviso genérico.
    actor = current_user.name or current_user.email
    body = "Sticker" if sticker else comment.content[:120]
    replied_to = parent.user_id if parent and parent.user_id != current_user.id else None
    if replied_to:
        realtime.notify_user(
            replied_to,
            title=f"{actor} respondió a tu comentario",
            body=body,
            url=f"/tasks/{task.id}#comment-{comment.id}",
            kind="task_comment",
        )
    for uid in _followers(task):
        if uid and str(uid) != str(current_user.id) and uid != replied_to:
            realtime.notify_user(
                uid,
                title=f"{actor} respondió un comentario" if parent else f"{actor} comentó",
                body=body,
                url=f"/tasks/{task.id}#comment-{comment.id}",
                kind="task_comment",
            )
    realtime.notify_task(task.id)
    audit.log_action(
        "task_comment", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=("respuesta: " if parent else "") + ("sticker" if sticker else (comment.content or "")[:80]),
    )

    # Una respuesta se agrega al hilo; un comentario raíz llega como hilo nuevo.
    return templates.TemplateResponse(
        request,
        "tasks/_comment.html" if parent else "tasks/_comment_thread.html",
        {
            "comment": comment,
            "replies": [],
            "task_id": task.id,
            "can_comment": True,
            "current_user": current_user,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.post("/{task_id}/comments/{comment_id}/edit", response_class=HTMLResponse)
def edit_comment(
    task_id: str,
    comment_id: str,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    comment = db.query(TaskComment).filter(
        TaskComment.id == comment_id, TaskComment.task_id == task_id
    ).first()
    if not comment:
        raise HTTPException(404)
    # Solo el autor puede editar su comentario.
    if str(comment.user_id) != str(current_user.id):
        raise HTTPException(403)
    if stickers.is_sticker(comment.content):
        raise HTTPException(400, "Un sticker no se puede editar")

    new_content = content.strip()
    if not new_content:
        raise HTTPException(400, "El comentario no puede quedar vacío")
    comment.content = new_content
    comment.edited = True
    db.commit()
    db.refresh(comment)
    comment.user = current_user  # for template rendering
    realtime.notify_task(task_id)

    return templates.TemplateResponse(
        request,
        "tasks/_comment.html",
        {
            "comment": comment,
            "current_user": current_user,
            "csrf_token": generate_csrf_token(str(current_user.id)),
            "comment_reactions": reactions.summarize(db, TaskCommentReaction, [comment.id], current_user.id),
        },
    )


@router.post("/{task_id}/comments/{comment_id}/delete", response_class=HTMLResponse)
def delete_comment(
    task_id: str,
    comment_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    comment = db.query(TaskComment).filter(
        TaskComment.id == comment_id, TaskComment.task_id == task_id
    ).first()
    if not comment:
        raise HTTPException(404)
    task = db.query(Task).filter(Task.id == task_id).first()
    # Borra el autor, o quien pueda editar la tarea (creador/admin del depto/superadmin).
    is_author = str(comment.user_id) == str(current_user.id)
    if not (is_author or (task and _can_edit_task(current_user, task, db))):
        raise HTTPException(403)

    db.delete(comment)
    db.commit()
    realtime.notify_task(task_id)
    # Cuerpo vacío + hx-swap="outerHTML" → HTMX elimina el comentario del DOM.
    return HTMLResponse("")


def _task_comment(task_id: str, comment_id: str, db: Session) -> TaskComment:
    try:
        uuid.UUID(comment_id)
    except ValueError:
        raise HTTPException(404)
    comment = db.query(TaskComment).filter(TaskComment.id == comment_id, TaskComment.task_id == task_id).first()
    if not comment:
        raise HTTPException(404)
    return comment


def _render_comment_reactions(request: Request, comment: TaskComment, current_user: User, db: Session):
    summary = reactions.summarize(db, TaskCommentReaction, [comment.id], current_user.id)
    return templates.TemplateResponse(
        request,
        "_reactions.html",
        {
            "rx_id": comment.id,
            "rx_list": summary.get(comment.id, []),
            "rx_url": f"/tasks/{comment.task_id}/comments/{comment.id}/reactions",
            "rx_class": "mt-1.5",
            "rx_readonly": not _can_comment(current_user, comment.task_id, db),
        },
    )


@router.get("/{task_id}/comments/{comment_id}/reactions", response_class=HTMLResponse)
def comment_reactions(
    task_id: str,
    comment_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Bloque de reacciones de un comentario (lo pide el detalle al recibir `event: reaction`)."""
    if not current_user:
        raise HTTPException(401)
    comment = _task_comment(task_id, comment_id, db)
    if not _can_view_task(current_user, comment.task_id, db):
        raise HTTPException(403)
    return _render_comment_reactions(request, comment, current_user, db)


@router.post("/{task_id}/comments/{comment_id}/reactions", response_class=HTMLResponse)
def toggle_comment_reaction(
    task_id: str,
    comment_id: str,
    request: Request,
    emoji: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    comment = _task_comment(task_id, comment_id, db)
    # Reacciona quien puede comentar (el auditor fuera de su alcance solo lee).
    if not _can_comment(current_user, comment.task_id, db):
        raise HTTPException(403)
    reactions.toggle(db, TaskCommentReaction, comment.id, current_user.id, emoji)
    # Evento propio en el canal de la tarea: el detalle refresca solo este bloque
    # en vez del location.reload() que provoca notify_task.
    realtime.notify_reaction(realtime.task_channel(comment.task_id), comment.id)
    return _render_comment_reactions(request, comment, current_user, db)
