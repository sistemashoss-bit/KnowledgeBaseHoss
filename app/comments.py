"""Comentarios de tareas, tickets y eventos del calendario, y sus reacciones.

Las tres tablas tienen la misma forma (user_id, content, edited, created_at y,
en tareas y tickets, parent_id para hilos de un nivel); aquí vive lo que hacen
igual: buscar, validar el texto o el sticker, crear, editar, armar hilos, avisar
y pintar el bloque de reacciones. Quién puede comentar, editar o borrar, y a
quién se avisa, lo decide cada módulo.
"""
import uuid

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app import reactions
from app.messaging import realtime, stickers
from app.templating import templates

EMPTY = "El comentario no puede quedar vacío"


def get_or_404(db: Session, model, owner_column, owner_id, comment_id: str):
    """El comentario `comment_id` de ese registro, o 404."""
    try:
        cid = uuid.UUID(str(comment_id))
    except ValueError:
        raise HTTPException(404)
    comment = db.query(model).filter(model.id == cid, owner_column == owner_id).first()
    if not comment:
        raise HTTPException(404)
    return comment


def thread_root(db: Session, model, parent):
    """Una respuesta siempre cuelga del comentario raíz (hilos de un solo nivel)."""
    if parent is not None and parent.parent_id:
        return db.get(model, parent.parent_id) or parent
    return parent


def clean(content: str, sticker: str = "", limit: int | None = None) -> str:
    """Texto a guardar: la URL del APNG si es sticker (como en el chat) o el texto
    sin espacios sobrantes. 400 si queda vacío."""
    text = stickers.require_url(sticker) if sticker else (content or "").strip()
    if limit and not sticker:
        text = text[:limit]
    if not text:
        raise HTTPException(400, EMPTY)
    return text


def create(db: Session, model, user, content: str, parent=None, **owner):
    """Agrega el comentario (sin commit). `owner`: la llave del registro, p. ej. task_id=…"""
    fields = {"id": uuid.uuid4(), "user_id": user.id, "content": content, **owner}
    if parent is not None:
        fields["parent_id"] = parent.id
    comment = model(**fields)
    db.add(comment)
    return comment


def edit(comment, user, content: str, limit: int | None = None) -> None:
    """Solo el autor, y un sticker no se edita. Marca `edited` solo si cambió."""
    if str(comment.user_id) != str(user.id):
        raise HTTPException(403)
    if stickers.is_sticker(comment.content):
        raise HTTPException(400, "Un sticker no se puede editar")
    text = clean(content, limit=limit)
    if text != comment.content:
        comment.content = text
        comment.edited = True


def threads(comments: list) -> list[tuple]:
    """[(raíz, respuestas)] en orden cronológico. Una respuesta cuyo raíz ya no
    está (borrado → SET NULL, o fuera de la lista) se muestra como raíz."""
    ids = {c.id for c in comments}
    replies: dict = {}
    for c in comments:
        if c.parent_id and c.parent_id in ids:
            replies.setdefault(c.parent_id, []).append(c)
    return [(c, replies.get(c.id, [])) for c in comments if not (c.parent_id and c.parent_id in ids)]


def notify(
    actor, followers, *, title: str, body: str, url: str, kind: str, parent=None, reply_body: str | None = None,
) -> None:
    """Avisa a los seguidores (menos a quien comenta). En una respuesta, el autor
    del comentario raíz recibe "respondió a tu comentario" en su lugar."""
    name = actor.name or actor.email
    replied_to = parent.user_id if parent is not None and parent.user_id != actor.id else None
    if replied_to:
        realtime.notify_user(
            replied_to, title=f"{name} respondió a tu comentario", body=reply_body or body, url=url, kind=kind,
        )
    ids = [uid for uid in dict.fromkeys(followers) if uid and str(uid) != str(actor.id) and uid != replied_to]
    if ids:
        realtime.notify_users(ids, title=title, body=body, url=url, kind=kind)


def reactions_block(request: Request, db: Session, reaction_model, comment, user, *, url: str, readonly: bool):
    """El bloque `_reactions.html` de un comentario (tareas y tickets)."""
    summary = reactions.summarize(db, reaction_model, [comment.id], user.id)
    return templates.TemplateResponse(request, "_reactions.html", {
        "rx_id": comment.id,
        "rx_list": summary.get(comment.id, []),
        "rx_url": url,
        "rx_class": "mt-1.5",
        "rx_readonly": readonly,
    })
