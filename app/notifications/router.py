from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import ConversationParticipant, Message

router = APIRouter(prefix="/api", tags=["api"])


# Las notificaciones (tareas, comentarios, etc.) viven en notificationservice y
# se consultan desde HossExtension; aquí solo queda el contador del badge de
# Mensajes del sidebar.
@router.get("/messages/unread-count")
def get_unread_conversations(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    count = (
        db.query(func.count(func.distinct(ConversationParticipant.conversation_id)))
        .join(Message, Message.conversation_id == ConversationParticipant.conversation_id)
        .filter(
            ConversationParticipant.user_id == current_user.id,
            Message.sender_id != current_user.id,
            or_(
                ConversationParticipant.last_read_at.is_(None),
                Message.created_at > ConversationParticipant.last_read_at,
            ),
        )
        .scalar()
    )
    return {"count": count or 0}
