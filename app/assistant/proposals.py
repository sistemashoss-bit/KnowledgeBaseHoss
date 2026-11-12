"""Propuestas de MAX (tarea, seguimiento, ticket): se guardan con su mensaje y se
ejecutan solo cuando quien lo invocó las confirma, con SUS permisos (las mismas
validaciones que la web)."""
import logging
import re
import uuid
from datetime import date, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import audit, storage
from app.calendar.reminders import to_local
from app.models import (
    AssistantProposal, ConversationParticipant, Message, MessageAttachment, Task, TaskEvidence, TicketEvidence, User,
    PROPOSAL_CONFIRMED, PROPOSAL_DISCARDED, PROPOSAL_FAILED, PROPOSAL_FOLLOW_UP, PROPOSAL_PENDING, PROPOSAL_TASK,
    PROPOSAL_TICKET,
)
from app.messaging import realtime
from app.tasks import service as task_service
from app.tickets import service as ticket_service

SOURCE_APP = "assistant"

logger = logging.getLogger(__name__)


def save(db: Session, message: Message | None, source_message_id, invoker_id, proposals: list[dict],
         *, source_text: str = "") -> list[AssistantProposal]:
    """Guarda las propuestas de una respuesta (`message` None = widget; entonces
    `source_text` es la pregunta, para `Task.context`). Las de seguimiento que
    apuntan a una tarea propuesta en la misma respuesta (`task_ref`, índice en
    la lista) se ligan por id de propuesta. Sin commit."""
    rows: list[AssistantProposal] = []
    for p in proposals:
        payload = dict(p["payload"])
        if message is None and source_text:
            payload["source_text"] = source_text[:2000]
        rows.append(AssistantProposal(
            id=uuid.uuid4(),
            message_id=message.id if message else None,
            conversation_id=message.conversation_id if message else None,
            source_message_id=source_message_id,
            invoker_id=invoker_id,
            kind=p["kind"],
            payload=payload,
            status=PROPOSAL_PENDING,
        ))
    for row in rows:
        ref = row.payload.get("task_ref")
        if ref is not None and 0 <= ref < len(rows):
            row.payload = {**row.payload, "task_proposal_id": str(rows[ref].id)}
    for row in rows:
        db.add(row)
    return rows


def for_messages(db: Session, message_ids: list) -> dict[str, list[AssistantProposal]]:
    """Propuestas por mensaje (para pintar las tarjetas en el feed)."""
    if not message_ids:
        return {}
    out: dict[str, list[AssistantProposal]] = {}
    rows = (
        db.query(AssistantProposal)
        .filter(AssistantProposal.message_id.in_(message_ids))
        .order_by(AssistantProposal.created_at)
        .all()
    )
    for r in rows:
        out.setdefault(str(r.message_id), []).append(r)
    return out


def _load_for_actor(db: Session, proposal_id: str, user: User, *, lock: bool = False) -> AssistantProposal:
    try:
        pid = uuid.UUID(proposal_id)
    except ValueError:
        raise HTTPException(404)
    q = db.query(AssistantProposal).filter(AssistantProposal.id == pid)
    if lock:
        # Doble clic en "Confirmar": la segunda petición espera y ya la ve resuelta.
        q = q.with_for_update()
    p = q.first()
    if not p:
        raise HTTPException(404)
    if str(p.invoker_id) != str(user.id):
        raise HTTPException(403, "Solo quien invocó a MAX puede confirmar o descartar esta propuesta")
    if p.conversation_id is None:
        return p  # widget: privada, basta con ser quien preguntó
    in_chat = db.query(ConversationParticipant.user_id).filter(
        ConversationParticipant.conversation_id == p.conversation_id,
        ConversationParticipant.user_id == user.id,
    ).first()
    if not in_chat:
        raise HTTPException(403)
    return p


def _source_context(p: AssistantProposal, db: Session) -> dict:
    if p.conversation_id is None:
        return {
            "source": "widget",
            "message_text": p.payload.get("source_text"),
            "proposal_id": str(p.id),
        }
    source = db.query(Message).filter(Message.id == p.source_message_id).first() if p.source_message_id else None
    return {
        "source": "chat",
        "conversation_id": str(p.conversation_id),
        "message_id": str(p.source_message_id) if p.source_message_id else None,
        "message_text": (source.content or "")[:2000] if source else None,
        "proposal_id": str(p.id),
    }


def confirm(db: Session, proposal_id: str, user: User, request=None) -> AssistantProposal:
    """Ejecuta la propuesta como `user`. Un error de permisos o de validación la
    deja en `failed` con el motivo (se ve en la tarjeta)."""
    p = _load_for_actor(db, proposal_id, user, lock=True)
    if p.status != PROPOSAL_PENDING:
        return p
    try:
        if p.kind == PROPOSAL_TASK:
            _confirm_task(db, p, user, request)
        elif p.kind == PROPOSAL_FOLLOW_UP:
            _confirm_follow_up(db, p, user, request)
        elif p.kind == PROPOSAL_TICKET:
            _confirm_ticket(db, p, user, request)
        else:
            raise HTTPException(422, "Propuesta desconocida")
    except HTTPException as exc:
        db.rollback()
        p = db.query(AssistantProposal).filter(AssistantProposal.id == p.id).first()
        p.status = PROPOSAL_FAILED
        p.error = str(exc.detail)[:300]
        p.resolved_at = datetime.utcnow()
        db.commit()
    return p


def _copy_attachments(db: Session, p: AssistantProposal, user: User, target) -> tuple[list[str], list[str]]:
    """Copia al bucket de evidencias los adjuntos del chat que trae la propuesta y
    los registra en la tarea o ticket recién creado (`target`). Solo adjuntos de
    mensajes vigentes del mismo chat. Devuelve (agregados, fallidos) por nombre;
    un fallo no deshace la tarea/ticket, se avisa en la tarjeta. Sin commit."""
    added, failed = [], []
    is_task = isinstance(target, Task)
    for item in p.payload.get("attachments") or []:
        name = item.get("filename") or "archivo"
        try:
            att_id = uuid.UUID(str(item.get("id")))
        except ValueError:
            failed.append(name)
            continue
        att = (
            db.query(MessageAttachment).join(Message, Message.id == MessageAttachment.message_id)
            .filter(
                MessageAttachment.id == att_id,
                Message.conversation_id == p.conversation_id,
                Message.deleted_at.is_(None),
            ).first()
        ) if p.conversation_id else None
        if att is None:
            failed.append(name)  # mensaje eliminado o adjunto ya purgado por la retención
            continue
        safe = re.sub(r"[^\w.\-]", "_", att.filename)[:200] or "file"
        key = f"{'tasks' if is_task else 'tickets'}/{target.id}/{uuid.uuid4()}_{safe}"
        try:
            storage.copy_chat_file_to_evidence(att.file_key, key)
        except Exception:
            logger.exception("assistant: no se pudo copiar el adjunto %s a %s", att.id, key)
            failed.append(name)
            continue
        model = TaskEvidence if is_task else TicketEvidence
        fk = {"task_id": target.id} if is_task else {"ticket_id": target.id}
        db.add(model(
            id=uuid.uuid4(), uploaded_by=user.id, filename=att.filename, file_key=key,
            content_type=att.content_type, file_size=att.file_size, **fk,
        ))
        added.append(att.filename)
    return added, failed


def _attachments_note(added: list[str], failed: list[str]) -> str:
    out = f" attachments={len(added)}" if added else ""
    return out + (f" attachments_failed={len(failed)}" if failed else "")


def _confirm_task(db: Session, p: AssistantProposal, user: User, request) -> None:
    data = p.payload
    task = task_service.create_task(
        db, user,
        title=data.get("title", ""),
        description=data.get("description", ""),
        priority=data.get("priority", "medium"),
        assigned_to=data.get("assigned_to", ""),
        due_date=date.fromisoformat(data["due_date"]) if data.get("due_date") else None,
        source_app=SOURCE_APP,
        context=_source_context(p, db),
    )
    added, failed = _copy_attachments(db, p, user, task)
    p.status = PROPOSAL_CONFIRMED
    p.task_id = task.id
    p.resolved_at = datetime.utcnow()
    if added or failed:
        p.payload = {**data, "attachments_added": added, "attachments_failed": failed}
    db.commit()
    task_service.notify_task_created(task, user)
    audit.log_action(
        "task_create", user=user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"priority={task.priority} via=assistant proposal={p.id}{_attachments_note(added, failed)}",
    )


def _confirm_follow_up(db: Session, p: AssistantProposal, user: User, request) -> None:
    data = p.payload
    task_id = data.get("task_id")
    if not task_id and data.get("task_proposal_id"):
        ref = db.query(AssistantProposal).filter(AssistantProposal.id == data["task_proposal_id"]).first()
        if not ref or ref.status != PROPOSAL_CONFIRMED or not ref.task_id:
            raise HTTPException(422, "Primero confirma la tarea de este seguimiento")
        task_id = ref.task_id
    task = db.query(Task).filter(Task.id == task_id).first() if task_id else None
    if not task:
        raise HTTPException(404, "La tarea ya no existe")
    result = task_service.create_follow_ups(
        db, user, task, day=data["day"], start=data["start"], end=data["end"], note=data.get("note", ""),
    )
    p.status = PROPOSAL_CONFIRMED
    p.task_id = task.id
    p.resolved_at = datetime.utcnow()
    db.commit()
    task_service.notify_follow_ups_created(task, user, result)
    audit.log_action(
        "task_follow_up_add", user=user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"{to_local(result['start_at']):%Y-%m-%d %H:%M}-{to_local(result['end_at']):%H:%M} via=assistant",
    )


def _confirm_ticket(db: Session, p: AssistantProposal, user: User, request) -> None:
    """Igual que levantarlo desde /tickets/new: queda a nombre de quien confirma
    y avisa a los admins del departamento (nace sin asignar)."""
    data = p.payload
    t = ticket_service.create_ticket(
        db, user,
        kind=data.get("kind", ""),
        title=data.get("title", ""),
        description=data.get("description", ""),
        department_id=data.get("department_id", ""),
        priority=data.get("priority", "medium"),
        source_app=SOURCE_APP,
        context=_source_context(p, db),
    )
    added, failed = _copy_attachments(db, p, user, t)
    p.status = PROPOSAL_CONFIRMED
    # La tarjeta enlaza al ticket por su número (no hay columna para tickets).
    p.payload = {**data, "ticket_number": t.number}
    if added or failed:
        p.payload = {**p.payload, "attachments_added": added, "attachments_failed": failed}
    p.resolved_at = datetime.utcnow()
    db.commit()
    admin_ids = ticket_service.department_admin_ids(t.department_id, db, exclude=user.id)
    if admin_ids:
        realtime.notify_users(
            admin_ids, title=f"Nuevo ticket de {user.name or user.email}",
            body=f"#{t.number} {t.title}", url=f"/tickets/{t.number}", kind="ticket_created",
        )
    audit.log_action(
        "ticket_create", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=f"#{t.number} {t.title}",
        details=f"kind={t.kind} priority={t.priority} via=assistant proposal={p.id}{_attachments_note(added, failed)}",
    )


def discard(db: Session, proposal_id: str, user: User) -> AssistantProposal:
    p = _load_for_actor(db, proposal_id, user)
    if p.status == PROPOSAL_PENDING:
        p.status = PROPOSAL_DISCARDED
        p.resolved_at = datetime.utcnow()
        db.commit()
    return p
