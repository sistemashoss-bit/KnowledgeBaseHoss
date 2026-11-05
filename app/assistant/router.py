"""Confirmar o descartar una propuesta de MAX desde la tarjeta del mensaje.

Responde la fila del mensaje ya actualizada (htmx la reemplaza) y avisa al resto
del chat con `msgupdate` para que también la vean resuelta."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import audit
from app.assistant import proposals
from app.auth.deps import get_current_user
from app.database import get_db
from app.messaging import realtime
from app.models import Message

router = APIRouter(prefix="/assistant", tags=["assistant"])


def _reply(request: Request, p, user, db: Session):
    from app.messaging.router import _render_message

    realtime.notify_message_update(p.conversation_id, p.message_id)
    msg = db.query(Message).filter(Message.id == p.message_id).first()
    if not msg:
        return HTMLResponse("")
    return _render_message(request, msg, user, db)


@router.post("/proposals/{proposal_id}/confirm", response_class=HTMLResponse)
def confirm_proposal(
    proposal_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    p = proposals.confirm(db, proposal_id, current_user, request)
    return _reply(request, p, current_user, db)


@router.post("/proposals/{proposal_id}/discard", response_class=HTMLResponse)
def discard_proposal(
    proposal_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    p = proposals.discard(db, proposal_id, current_user)
    audit.log_action(
        "assistant_proposal_discard", user=current_user, request=request,
        resource_type="conversation", resource_id=str(p.conversation_id), details=f"proposal={p.id} kind={p.kind}",
    )
    return _reply(request, p, current_user, db)
