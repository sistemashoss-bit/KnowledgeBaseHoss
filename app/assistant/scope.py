"""Lo que MAX puede usar en una conversación: lo que ven TODOS sus participantes.

La respuesta se publica en el chat y la leen todos, así que cada consulta se
filtra con la intersección de los permisos de cada participante (sin contar al
bot). Para acciones (proponer/crear) además manda el permiso de quien invocó.
"""
from dataclasses import dataclass, field

from sqlalchemy import and_, func
from sqlalchemy.orm import Session, joinedload

from app.models import Conversation, ConversationParticipant, Task, User
from app.permissions import build_access_filter, can_access_document
from app.tasks.service import task_view_condition


@dataclass
class Scope:
    conversation: Conversation
    invoker: User
    participants: list[User] = field(default_factory=list)

    @property
    def participant_ids(self) -> set:
        return {u.id for u in self.participants}


def load_scope(db: Session, conv_id, invoker_id) -> Scope | None:
    conv = db.query(Conversation).filter(Conversation.id == conv_id).first()
    invoker = db.query(User).filter(User.id == invoker_id).first()
    if not conv or not invoker:
        return None
    participants = (
        db.query(User)
        .options(joinedload(User.department), joinedload(User.department_access))
        .join(ConversationParticipant, ConversationParticipant.user_id == User.id)
        .filter(
            ConversationParticipant.conversation_id == conv.id,
            User.is_bot.is_(False),
            User.is_active.is_(True),
        )
        .all()
    )
    if invoker.id not in {u.id for u in participants}:
        return None  # ya no está en el chat
    return Scope(conversation=conv, invoker=invoker, participants=participants)


# ── Documentos ────────────────────────────────────────────────────────────────

def documents_filter(scope: Scope) -> dict:
    """Filtro de OpenSearch: el documento debe pasar el filtro de cada participante."""
    filters = [build_access_filter(u) for u in scope.participants]
    filters = [f for f in filters if "match_all" not in f]
    if not filters:
        return {"match_all": {}}
    return {"bool": {"must": filters}}


def document_visible_to_all(scope: Scope, doc) -> bool:
    """Revisión final contra la BD (el índice se actualiza en segundo plano)."""
    return all(can_access_document(u, doc) for u in scope.participants)


# ── Tareas ────────────────────────────────────────────────────────────────────

def tasks_condition(scope: Scope):
    """Condición SQL de las tareas que ven todos los participantes, o None (todas)."""
    conds = [c for c in (task_view_condition(u) for u in scope.participants) if c is not None]
    if not conds:
        return None
    return and_(*conds)


def task_visible_to_all(db: Session, scope: Scope, task_id) -> Task | None:
    q = db.query(Task).filter(Task.id == task_id)
    cond = tasks_condition(scope)
    if cond is not None:
        q = q.filter(cond)
    return q.first()


# ── Conversaciones ────────────────────────────────────────────────────────────

def shared_conversation_ids(db: Session, scope: Scope) -> list:
    """Conversaciones donde están TODOS los participantes del chat actual (el
    actual incluido): lo que se lea de ahí ya lo ve cada uno."""
    ids = list(scope.participant_ids)
    if not ids:
        return []
    rows = (
        db.query(ConversationParticipant.conversation_id)
        .filter(ConversationParticipant.user_id.in_(ids))
        .group_by(ConversationParticipant.conversation_id)
        .having(func.count(func.distinct(ConversationParticipant.user_id)) == len(ids))
        .all()
    )
    return [r[0] for r in rows]
