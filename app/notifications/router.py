from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    AuditLog, Conversation, ConversationParticipant, Message, Task, User,
    CONV_GROUP, ROLE_ADMIN, ROLE_SUPERADMIN,
)

router = APIRouter(prefix="/api", tags=["api"])

CUTOFF_HOURS = 48


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _names_for_ids(db: Session, ids) -> dict:
    """Mapa user_id(str) → nombre visible (name o email) para los ids dados."""
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        str(u.id): (u.name or u.email)
        for u in db.query(User).filter(User.id.in_(ids)).all()
    }


def _actor_names(db: Session, logs: list) -> dict:
    """Igual que _names_for_ids pero tomando el actor (user_id) de una lista de logs."""
    return _names_for_ids(db, {l.user_id for l in logs if l.user_id})


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

    creator_names = _names_for_ids(db, {t.created_by for t in my_new_tasks})
    for task in my_new_tasks:
        actor = creator_names.get(str(task.created_by))
        notifications.append({
            "id": f"task_new_{task.id}",
            "type": "task_assigned",
            "title": f"{actor} te asignó una tarea" if actor else "Tarea asignada",
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
        dept_creator_names = _names_for_ids(db, {t.created_by for t in dept_tasks})
        for task in dept_tasks:
            actor = dept_creator_names.get(str(task.created_by))
            notifications.append({
                "id": f"task_dept_{task.id}",
                "type": "task_dept",
                "title": "Nueva tarea en tu departamento",
                "subtitle": f"{actor} · {task.title}" if actor else task.title,
                "url": f"/tasks/{task.id}",
                "created_at": _fmt(task.created_at),
            })

    # Tareas que "vigilo": donde soy asignado o creador; y para admin (con depto),
    # también las de su departamento. Base de estados/comentarios/evidencias.
    watched = {
        str(t.id) for t in db.query(Task.id).filter(
            or_(Task.assigned_to == current_user.id, Task.created_by == current_user.id)
        ).all()
    }
    if current_user.role == ROLE_ADMIN and current_user.department_id:
        watched.update(
            str(t.id) for t in db.query(Task.id).filter(
                Task.department_id == current_user.department_id
            ).all()
        )
    watched_ids = list(watched)

    # ── 3. Cambios de estado (de otras personas) ─────────────────────────────
    if watched_ids:
        status_logs = db.query(AuditLog).filter(
            AuditLog.action == "task_status_change",
            AuditLog.resource_type == "task",
            AuditLog.resource_id.in_(watched_ids),
            AuditLog.user_email != current_user.email,
            AuditLog.created_at > cutoff,
        ).order_by(AuditLog.created_at.desc()).all()
        names = _actor_names(db, status_logs)
        for log in status_logs:
            actor = names.get(str(log.user_id)) or log.user_email or "Alguien"
            notifications.append({
                "id": f"task_status_{log.id}",
                "type": "task_status",
                "title": f"{actor} cambió el estado",
                "subtitle": f"{log.resource_name}: {log.details}",
                "url": f"/tasks/{log.resource_id}",
                "created_at": _fmt(log.created_at),
            })

    # ── 3b. Comentarios nuevos (de otras personas) ───────────────────────────
    if watched_ids:
        comment_logs = db.query(AuditLog).filter(
            AuditLog.action == "task_comment",
            AuditLog.resource_type == "task",
            AuditLog.resource_id.in_(watched_ids),
            AuditLog.user_email != current_user.email,
            AuditLog.created_at > cutoff,
        ).order_by(AuditLog.created_at.desc()).all()
        names = _actor_names(db, comment_logs)
        for log in comment_logs:
            actor = names.get(str(log.user_id)) or log.user_email or "Alguien"
            notifications.append({
                "id": f"task_comment_{log.id}",
                "type": "task_comment",
                "title": f"{actor} comentó",
                "subtitle": log.resource_name or "",
                "url": f"/tasks/{log.resource_id}",
                "created_at": _fmt(log.created_at),
            })

    # ── 3c. Evidencias nuevas (de otras personas) ────────────────────────────
    if watched_ids:
        evidence_logs = db.query(AuditLog).filter(
            AuditLog.action == "evidence_upload",
            AuditLog.resource_type == "task",
            AuditLog.resource_id.in_(watched_ids),
            AuditLog.user_email != current_user.email,
            AuditLog.created_at > cutoff,
        ).order_by(AuditLog.created_at.desc()).all()
        names = _actor_names(db, evidence_logs)
        for log in evidence_logs:
            actor = names.get(str(log.user_id)) or log.user_email or "Alguien"
            notifications.append({
                "id": f"task_evidence_{log.id}",
                "type": "task_evidence",
                "title": f"{actor} subió evidencia",
                "subtitle": log.resource_name or "",
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
    reassign_names = _actor_names(db, reassign_logs)
    for log in reassign_logs:
        if log.resource_id not in my_new_task_ids:
            actor = reassign_names.get(str(log.user_id)) or log.user_email
            notifications.append({
                "id": f"task_reassign_{log.id}",
                "type": "task_assigned",
                "title": f"{actor} te reasignó una tarea" if actor else "Tarea reasignada a ti",
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
            .options(joinedload(Message.sender))
            .filter(Message.conversation_id == part.conversation_id)
            .order_by(Message.created_at.desc())
            .first()
        )
        # En grupos, indica quién envió el último mensaje (en directos ya es la persona).
        if conv.type == CONV_GROUP and last_msg and last_msg.sender:
            sender = last_msg.sender.name or last_msg.sender.email
            display_name = f"{display_name} · {sender}"

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
