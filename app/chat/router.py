from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.permissions import build_access_filter
from app import audit, rag
from app.assistant import widget as assistant_widget
from app.templating import templates

router = APIRouter(prefix="/chat", tags=["chat"])


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
        # Landing público: solo documentos públicos, como siempre.
        result = rag.answer_question(question.strip(), build_access_filter(None), role="anon")
        audit.log_search(question.strip(), user=None, result_count=len(result["sources"]), search_type="rag")

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
