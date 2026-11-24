"""Encuestas rápidas (como las de WhatsApp): una pregunta, de 2 a 12 opciones,
voto único o múltiple, se puede cambiar o quitar el voto y se ve quién votó.

Módulo independiente de cualquier sección: no tiene rutas ni revisa permisos.
Quien lo usa guarda el vínculo en su propia tabla (el chat: Message.poll_id),
valida que la persona pueda votar y avisa en su canal de tiempo real. La
tarjeta es templates/polls/_card.html.
"""
import uuid
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Poll, PollOption, PollVote, User

MIN_OPTIONS = 2
MAX_OPTIONS = 12
QUESTION_MAX = 300
OPTION_MAX = 150


# Texto del mensaje que solo lleva la encuesta (lista de chats, avisos, búsqueda).
# Si el mensaje dice otra cosa (p. ej. la respuesta de MAX), se dibuja el texto
# y la tarjeta debajo (messaging/_messages.html compara con este prefijo).
MESSAGE_PREFIX = "📊 Encuesta: "


def message_text(poll: Poll) -> str:
    return f"{MESSAGE_PREFIX}{poll.question}"


def _clean(text: str | None, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def validate(question: str, options: list[str]) -> tuple[str, list[str]]:
    """Pregunta y opciones limpias, o HTTPException 422 con el motivo. Las
    opciones vacías se ignoran y las repetidas (sin distinguir mayúsculas)
    cuentan una vez."""
    question = _clean(question, QUESTION_MAX)
    if not question:
        raise HTTPException(422, "Escribe la pregunta.")
    texts, seen = [], set()
    for raw in options:
        text = _clean(raw, OPTION_MAX)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            texts.append(text)
    if len(texts) < MIN_OPTIONS:
        raise HTTPException(422, f"Agrega al menos {MIN_OPTIONS} opciones distintas.")
    if len(texts) > MAX_OPTIONS:
        raise HTTPException(422, f"Máximo {MAX_OPTIONS} opciones.")
    return question, texts


def create(db: Session, question: str, options: list[str], multiple: bool, user_id) -> Poll:
    """Valida (validate) y agrega la encuesta a la sesión (sin commit)."""
    question, texts = validate(question, options)
    poll = Poll(
        id=uuid.uuid4(), question=question, multiple=bool(multiple),
        created_by=user_id, created_at=datetime.utcnow(),
    )
    poll.options = [PollOption(id=uuid.uuid4(), text=t, position=i) for i, t in enumerate(texts)]
    db.add(poll)
    return poll


def clone(db: Session, poll: Poll, user_id) -> Poll:
    """Copia sin votos (reenviar una encuesta crea otra, como en WhatsApp)."""
    return create(db, poll.question, [o.text for o in poll.options], poll.multiple, user_id)


def vote(db: Session, poll_id, option_id: str, user_id) -> None:
    """Toca una opción: si ya era suya la quita; si no, la agrega (voto único:
    reemplaza la anterior). Hace commit."""
    poll = db.query(Poll).filter(Poll.id == poll_id).with_for_update().first()
    if not poll:
        raise HTTPException(404)
    if poll.closed_at:
        raise HTTPException(409, "La encuesta ya está cerrada.")
    try:
        option_uuid = uuid.UUID(str(option_id))
    except ValueError:
        raise HTTPException(400)
    if option_uuid not in {o.id for o in poll.options}:
        raise HTTPException(400)

    mine = db.query(PollVote).filter(PollVote.poll_id == poll.id, PollVote.user_id == user_id)
    if mine.filter(PollVote.option_id == option_uuid).delete(synchronize_session=False):
        db.commit()
        return
    if not poll.multiple:
        mine.delete(synchronize_session=False)
    db.add(PollVote(option_id=option_uuid, user_id=user_id, poll_id=poll.id, created_at=datetime.utcnow()))
    db.commit()


def delete(db: Session, poll_ids) -> None:
    """Borra encuestas con sus opciones y votos (sin commit)."""
    poll_ids = [p for p in poll_ids if p]
    if poll_ids:
        db.query(Poll).filter(Poll.id.in_(poll_ids)).delete(synchronize_session=False)


def summarize(db: Session, polls: list[Poll], user_id) -> dict:
    """{poll_id: resultados} para dibujar la tarjeta, en una consulta de votos:
    por opción, conteo, porcentaje (sobre quienes votaron), si es tuya y los
    nombres; además total de votantes."""
    polls = [p for p in polls if p]
    if not polls:
        return {}
    rows = (
        db.query(PollVote.poll_id, PollVote.option_id, PollVote.user_id, User.name, User.email)
        .join(User, User.id == PollVote.user_id)
        .filter(PollVote.poll_id.in_([p.id for p in polls]))
        .order_by(PollVote.created_at)
        .all()
    )
    by_option: dict = {}
    voters: dict = {}
    for poll_id, option_id, uid, name, email in rows:
        by_option.setdefault(option_id, []).append((uid, name or email))
        voters.setdefault(poll_id, set()).add(uid)

    out = {}
    for poll in polls:
        total = len(voters.get(poll.id, ()))
        options = []
        for opt in poll.options:
            votes = by_option.get(opt.id, [])
            options.append({
                "id": opt.id,
                "text": opt.text,
                "count": len(votes),
                "pct": round(100 * len(votes) / total) if total else 0,
                "mine": any(uid == user_id for uid, _ in votes),
                "voters": [name for _, name in votes],
            })
        top = max((o["count"] for o in options), default=0)
        for o in options:
            o["leading"] = top > 0 and o["count"] == top
        out[poll.id] = {"options": options, "voters": total, "voted": any(o["mine"] for o in options)}
    return out
