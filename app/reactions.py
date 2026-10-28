"""Reacciones con emoji a mensajes del chat y a comentarios (tareas, calendario).

Una tabla por tipo de objetivo (FK con ON DELETE CASCADE, así la purga del chat
y el borrado de comentarios las limpian solas) y la misma lógica para las tres:
se leen con UNA consulta agrupada por render, nunca con joinedload, para no
multiplicar filas en consultas que ya cargan muchas relaciones.
"""
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import REACTION_EMOJIS, User


def _target_col(model):
    """Columna que apunta al mensaje/comentario reaccionado (la PK que no es user/emoji)."""
    return next(c for c in model.__table__.primary_key.columns if c.name not in ("user_id", "emoji"))


def summarize(db: Session, model, target_ids, user_id) -> dict:
    """{target_id: [{emoji, count, mine, names}]} para los objetivos dados,
    en el orden de REACTION_EMOJIS. Los que no tienen reacciones no aparecen."""
    ids = list(target_ids)
    if not ids:
        return {}
    col = getattr(model, _target_col(model).name)
    rows = (
        db.query(
            col,
            model.emoji,
            func.count(),
            func.bool_or(model.user_id == user_id),
            func.string_agg(func.coalesce(User.name, User.email), ", "),
        )
        .join(User, User.id == model.user_id)
        .filter(col.in_(ids))
        .group_by(col, model.emoji)
        .all()
    )
    order = {e: n for n, e in enumerate(REACTION_EMOJIS)}
    result: dict = {}
    for target_id, emoji, count, mine, names in rows:
        result.setdefault(target_id, []).append(
            {"emoji": emoji, "count": count, "mine": bool(mine), "names": names or ""}
        )
    for items in result.values():
        items.sort(key=lambda r: order.get(r["emoji"], len(order)))
    return result


def toggle(db: Session, model, target_id, user_id, emoji: str) -> None:
    """Pone la reacción si no existía, o la quita si ya estaba. Hace commit."""
    if emoji not in REACTION_EMOJIS:
        raise HTTPException(400, "Reacción no válida")
    col_name = _target_col(model).name
    inserted = db.execute(
        insert(model.__table__)
        .values({col_name: target_id, "user_id": user_id, "emoji": emoji, "created_at": datetime.utcnow()})
        .on_conflict_do_nothing()
    ).rowcount
    if not inserted:
        db.query(model).filter(
            getattr(model, col_name) == target_id, model.user_id == user_id, model.emoji == emoji,
        ).delete(synchronize_session=False)
    db.commit()
