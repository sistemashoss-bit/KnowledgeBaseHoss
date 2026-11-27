"""Comentarios de un evento y sus reacciones (dueño, compartidos y quien ve su calendario)."""

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, joinedload

from app import audit, comments, reactions, storage
from app.auth.deps import get_current_user
from app.calendar.reminders import to_local
from app.database import get_db
from app.models import CalendarCommentReaction, CalendarEvent, CalendarEventComment, User
from app.messaging import stickers
from app.calendar.helpers import (
    _display_name,
    _initials,
    _visible_event,
)

router = APIRouter()

MAX_CHARS = 5000


def _comment_json(c: CalendarEventComment, user: User, ev: CalendarEvent, rx: list | None = None) -> dict:
    return {
        "id": str(c.id),
        "content": c.content,
        # Comentario-sticker: URL del APNG (se dibuja como imagen y no se edita).
        "sticker": c.content if stickers.is_sticker(c.content) else None,
        "author": _display_name(c.user),
        "initials": _initials(c.user),
        # CDN de Bunny, igual que avatar_url en las plantillas (404 → iniciales).
        "avatar_url": storage.get_avatar_url(c.user),
        "created_at": to_local(c.created_at).isoformat(timespec="minutes"),
        "edited": c.edited,
        # Edita solo el autor; borra el autor o el dueño del evento.
        "can_edit": c.user_id == user.id and not stickers.is_sticker(c.content),
        "can_delete": c.user_id == user.id or ev.owner_id == user.id,
        "reactions": rx or [],
    }


@router.get("/events/{event_id}/comments")
def list_comments(
    event_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    comments = (
        db.query(CalendarEventComment)
        .options(joinedload(CalendarEventComment.user))
        .filter(CalendarEventComment.event_id == ev.id)
        .order_by(CalendarEventComment.created_at)
        .all()
    )
    rx = reactions.summarize(db, CalendarCommentReaction, [c.id for c in comments], current_user.id)
    return JSONResponse([_comment_json(c, current_user, ev, rx.get(c.id)) for c in comments])


@router.post("/events/{event_id}/comments")
def add_comment(
    event_id: str,
    request: Request,
    content: str = Form(default=""),
    date: str = Form(""),
    sticker: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    content = comments.clean(content, sticker, limit=MAX_CHARS)
    comment = comments.create(db, CalendarEventComment, current_user, content, event_id=ev.id)
    db.commit()
    db.refresh(comment)

    # Avisa al dueño, a los compartidos y a quienes ya comentaron (menos a
    # quien comenta). `date`
    # es la ocurrencia desde la que se comentó, para abrir el calendario ahí.
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        day = to_local(ev.start_at).date()
    followers = {ev.owner_id} | {sh.user_id for sh in ev.shares} | {
        uid for (uid,) in db.query(CalendarEventComment.user_id).filter(CalendarEventComment.event_id == ev.id)
    }
    comments.notify(
        current_user, followers, title=f"{_display_name(current_user)} comentó en «{ev.title}»",
        body="Sticker" if sticker else content[:120],
        url=f"/calendar/?date={day.isoformat()}&event={ev.id}", kind="calendar_comment",
    )
    audit.log_action(
        "calendar_event_comment", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
        details="sticker" if sticker else content[:80],
    )
    return _comment_json(comment, current_user, ev)


def _event_comment(ev: CalendarEvent, comment_id: str, db: Session) -> CalendarEventComment:
    return comments.get_or_404(db, CalendarEventComment, CalendarEventComment.event_id, ev.id, comment_id)


@router.post("/events/{event_id}/comments/{comment_id}/edit")
def edit_comment(
    event_id: str,
    comment_id: str,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    comment = _event_comment(ev, comment_id, db)
    comments.edit(comment, current_user, content, limit=MAX_CHARS)
    db.commit()
    rx = reactions.summarize(db, CalendarCommentReaction, [comment.id], current_user.id)
    return _comment_json(comment, current_user, ev, rx.get(comment.id))


@router.post("/events/{event_id}/comments/{comment_id}/reactions")
def toggle_comment_reaction(
    event_id: str,
    comment_id: str,
    emoji: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Alterna una reacción; reacciona quien puede ver (y por tanto comentar) el evento."""
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    comment = _event_comment(ev, comment_id, db)
    reactions.toggle(db, CalendarCommentReaction, comment.id, current_user.id, emoji)
    rx = reactions.summarize(db, CalendarCommentReaction, [comment.id], current_user.id)
    return {"reactions": rx.get(comment.id, [])}


@router.post("/events/{event_id}/comments/{comment_id}/delete")
def delete_comment(
    event_id: str,
    comment_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    comment = _event_comment(ev, comment_id, db)
    if comment.user_id != current_user.id and ev.owner_id != current_user.id:
        raise HTTPException(403)
    db.delete(comment)
    db.commit()
    return {"ok": True}
