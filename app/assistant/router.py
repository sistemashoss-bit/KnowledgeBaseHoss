"""Confirmar o descartar una propuesta de MAX desde su tarjeta.

En el chat responde la fila del mensaje ya actualizada (htmx la reemplaza) y
avisa al resto con `msgupdate`. En el widget (`?mode=widget`) responde solo la
tarjeta, siempre con 200: si la tarjeta guardada en el navegador quedó vieja, se
pinta el estado real (el widget no muestra errores htmx)."""
from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import audit
from app.assistant import proposals
from app.auth.deps import get_current_user
from app.database import get_db
from app.messaging import realtime
from app.models import Message
from app.templating import templates

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _widget_card(request: Request, p, user):
    return templates.TemplateResponse(
        request, "assistant/_proposal_card.html", {"p": p, "card_mode": "widget", "current_user": user},
    )


def _widget_error(exc: HTTPException) -> HTMLResponse:
    detail = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else "Esta propuesta ya no existe."
    return HTMLResponse(f'<p class="text-xs text-red-600">{escape(detail)}</p>')


def _reply(request: Request, p, user, db: Session, mode: str = ""):
    from app.messaging.router import _render_message

    if mode == "widget" or p.conversation_id is None:
        return _widget_card(request, p, user)

    realtime.notify_message_update(p.conversation_id, p.message_id)
    msg = db.query(Message).filter(Message.id == p.message_id).first()
    if not msg:
        return HTMLResponse("")
    return _render_message(request, msg, user, db)


@router.post("/proposals/{proposal_id}/confirm", response_class=HTMLResponse)
def confirm_proposal(
    proposal_id: str,
    request: Request,
    mode: str = Query(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    try:
        p = proposals.confirm(db, proposal_id, current_user, request)
    except HTTPException as exc:
        if mode == "widget":
            return _widget_error(exc)
        raise
    return _reply(request, p, current_user, db, mode)


@router.post("/proposals/{proposal_id}/discard", response_class=HTMLResponse)
def discard_proposal(
    proposal_id: str,
    request: Request,
    mode: str = Query(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    try:
        p = proposals.discard(db, proposal_id, current_user)
    except HTTPException as exc:
        if mode == "widget":
            return _widget_error(exc)
        raise
    audit.log_action(
        "assistant_proposal_discard", user=current_user, request=request,
        resource_type="conversation" if p.conversation_id else "assistant",
        resource_id=str(p.conversation_id) if p.conversation_id else None,
        details=f"proposal={p.id} kind={p.kind}" + ("" if p.conversation_id else " via=widget"),
    )
    return _reply(request, p, current_user, db, mode)
