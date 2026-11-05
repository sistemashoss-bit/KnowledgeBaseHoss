"""El usuario bot (MAX) y cómo se detecta que lo invocaron."""
from types import SimpleNamespace

from sqlalchemy.orm import Session

from app import mentions
from app.models import ASSISTANT_BOT_ID, ASSISTANT_BOT_NAME, ConversationParticipant, Message, User

_BOT_REF = SimpleNamespace(id=ASSISTANT_BOT_ID, name=ASSISTANT_BOT_NAME, email="")


def is_bot(user) -> bool:
    return bool(user is not None and getattr(user, "is_bot", False))


def is_bot_id(user_id) -> bool:
    return user_id is not None and str(user_id) == str(ASSISTANT_BOT_ID)


def get_bot(db: Session) -> User | None:
    return db.query(User).filter(User.id == ASSISTANT_BOT_ID).first()


def mentioned_in(text: str) -> bool:
    """¿El texto menciona a MAX con su nombre exacto (`@MAX`)? `@todos` no cuenta."""
    ids, _everyone = mentions.find(text or "", [_BOT_REF])
    return ASSISTANT_BOT_ID in ids


def replies_to_bot(db: Session, msg_id) -> bool:
    """¿El mensaje es una respuesta ("Responder") a un mensaje de MAX? Así se le
    sigue la conversación sin volver a escribir @MAX."""
    reply_to_id = db.query(Message.reply_to_id).filter(Message.id == msg_id).scalar()
    if not reply_to_id:
        return False
    sender_id = db.query(Message.sender_id).filter(Message.id == reply_to_id).scalar()
    return is_bot_id(sender_id)


def add_to_conversation(conv_id, db: Session) -> None:
    """Agrega a MAX como participante (al crear un grupo). Sin commit."""
    if get_bot(db) is None:
        return  # migración 053 sin correr: no hay bot
    exists = db.query(ConversationParticipant.user_id).filter(
        ConversationParticipant.conversation_id == conv_id,
        ConversationParticipant.user_id == ASSISTANT_BOT_ID,
    ).first()
    if not exists:
        db.add(ConversationParticipant(conversation_id=conv_id, user_id=ASSISTANT_BOT_ID))
