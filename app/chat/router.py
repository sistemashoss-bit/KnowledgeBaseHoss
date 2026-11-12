import logging

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.permissions import build_access_filter
from app import audit, rag
from app.assistant import limits as assistant_limits
from app.assistant import widget as assistant_widget
from app.templating import templates

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str:
    """IP de quien pregunta. En producción Caddy pone en X-Forwarded-For la IP
    real (no confía en la que mande el cliente) y knowledge solo es accesible
    detrás de él; sin proxy, la del socket."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "desconocida")


@router.post("/", response_class=HTMLResponse)
def ask(
    request: Request,
    question: str = Form(...),
    mode: str = Query(default="full"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not question.strip():
        return HTMLResponse("")

    if user:
        # Con sesión: MAX con tools (documentos, tareas, chats) y los permisos del usuario.
        result = assistant_widget.answer(db, user, question.strip())
    else:
        # Landing público: solo documentos públicos, con tope de preguntas por IP.
        ip = _client_ip(request)
        exceeded = assistant_limits.check_ip(ip)
        if exceeded:
            result = {"answer": assistant_limits.limit_message(*exceeded), "sources": []}
        else:
            try:
                result = rag.answer_question(question.strip(), build_access_filter(None), role="anon")
                audit.log_search(question.strip(), user=None, result_count=len(result["sources"]), search_type="rag")
            except Exception:
                logger.exception("public RAG question failed")
                assistant_limits.refund_ip(ip)
                result = {"answer": "No pude responder en este momento; intenta de nuevo en un rato.", "sources": []}

    tpl = "chat/_widget_response.html" if mode == "widget" else "chat/_response.html"
    return templates.TemplateResponse(
        request, tpl,
        {
            "current_user": user,
            "question": question.strip(),
            "answer": result["answer"],
            "sources": result["sources"],
            "proposals": result.get("proposals", []),
        },
    )


@router.post("/clear")
def clear(user=Depends(get_current_user)):
    """Botón "Limpiar" del widget: olvida también la memoria de la conversación."""
    if user:
        assistant_widget.clear_history(user.id)
    return Response(status_code=204)
