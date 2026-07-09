from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    AuditLog, Conversation, ConversationParticipant, Message, Task,
    CONV_GROUP, ROLE_SUPERADMIN,
)

router = APIRouter(prefix="/api", tags=["api"])

CUTOFF_HOURS = 48


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/notifications")
def get_notifications(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    cutoff = datetime.utcnow() - timedelta(hours=CUTOFF_HOURS)
    notifications = []

    # ── 1. Tareas nuevas asignadas directamente a mí ─────────────────────────
    my_new_tasks = db.query(Task).filter(
        Task.assigned_to == current_user.id,
        Task.created_at > cutoff,
    ).all()
    my_new_task_ids = {str(t.id) for t in my_new_tasks}

    for task in my_new_tasks:
        notifications.append({
            "id": f"task_new_{task.id}",
            "type": "task_assigned",
            "title": "Tarea asignada",
            "subtitle": task.title,
            "url": f"/tasks/{task.id}",
            "created_at": _fmt(task.created_at),
        })

    # ── 2. Tareas nuevas del departamento (no asignadas específicamente a mí) ─
    if current_user.department_id and current_user.role != ROLE_SUPERADMIN:
        dept_tasks = db.query(Task).filter(
            Task.department_id == current_user.department_id,
            Task.created_at > cutoff,
            or_(Task.assigned_to != current_user.id, Task.assigned_to.is_(None)),
        ).all()
        for task in dept_tasks:
            notifications.append({
                "id": f"task_dept_{task.id}",
                "type": "task_dept",
                "title": "Nueva tarea en tu departamento",
                "subtitle": task.title,
                "url": f"/tasks/{task.id}",
                "created_at": _fmt(task.created_at),
            })

    # ── 3. Cambios de estado en tareas donde soy asignado o creador ──────────
    my_task_ids = [
        str(t.id) for t in db.query(Task.id).filter(
            or_(Task.assigned_to == current_user.id, Task.created_by == current_user.id)
        ).all()
    ]
    if my_task_ids:
        status_logs = db.query(AuditLog).filter(
            AuditLog.action == "task_status_change",
            AuditLog.resource_type == "task",
            AuditLog.resource_id.in_(my_task_ids),
            AuditLog.user_email != current_user.email,
            AuditLog.created_at > cutoff,
        ).order_by(AuditLog.created_at.desc()).all()
        for log in status_logs:
            notifications.append({
                "id": f"task_status_{log.id}",
                "type": "task_status",
                "title": "Cambio de estado",
                "subtitle": f"{log.resource_name}: {log.details}",
                "url": f"/tasks/{log.resource_id}",
                "created_at": _fmt(log.created_at),
            })

    # ── 4. Reasignación a mí (tarea existente, alguien me la asignó) ─────────
    reassign_logs = db.query(AuditLog).filter(
        AuditLog.action == "task_assign",
        AuditLog.resource_type == "task",
        AuditLog.details == f"assigned_to={current_user.email}",
        AuditLog.user_email != current_user.email,
        AuditLog.created_at > cutoff,
    ).order_by(AuditLog.created_at.desc()).all()
    for log in reassign_logs:
        if log.resource_id not in my_new_task_ids:
            notifications.append({
                "id": f"task_reassign_{log.id}",
                "type": "task_assigned",
                "title": "Tarea reasignada a ti",
                "subtitle": log.resource_name or "",
                "url": f"/tasks/{log.resource_id}",
                "created_at": _fmt(log.created_at),
            })

    # ── 5. Mensajes no leídos ─────────────────────────────────────────────────
    participations = (
        db.query(ConversationParticipant)
        .filter(ConversationParticipant.user_id == current_user.id)
        .all()
    )
    for part in participations:
        q = db.query(func.count(Message.id)).filter(
            Message.conversation_id == part.conversation_id,
            Message.sender_id != current_user.id,
        )
        if part.last_read_at:
            q = q.filter(Message.created_at > part.last_read_at)
        unread = q.scalar() or 0

        if unread == 0:
            continue

        conv = (
            db.query(Conversation)
            .options(joinedload(Conversation.participants).joinedload(ConversationParticipant.user))
            .filter(Conversation.id == part.conversation_id)
            .first()
        )
        if not conv:
            continue

        if conv.type == CONV_GROUP:
            display_name = conv.name or "Grupo sin nombre"
        else:
            other = next(
                (p.user for p in conv.participants if str(p.user_id) != str(current_user.id)),
                None,
            )
            display_name = (other.name or other.email) if other else "Chat directo"

        last_msg = (
            db.query(Message)
            .filter(Message.conversation_id == part.conversation_id)
            .order_by(Message.created_at.desc())
            .first()
        )
        palabra = "mensajes" if unread > 1 else "mensaje"
        ts = last_msg.created_at if last_msg else conv.created_at
        notifications.append({
            "id": f"msg_{conv.id}",
            "type": "message",
            "title": f"{unread} {palabra} sin leer",
            "subtitle": display_name,
            "url": f"/messaging/{conv.id}",
            "created_at": _fmt(ts),
        })

    notifications.sort(key=lambda x: x["created_at"], reverse=True)
    return JSONResponse(content=notifications)
