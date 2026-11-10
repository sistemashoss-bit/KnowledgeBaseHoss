"""Herramientas: utilidades de uso general (menú "Herramientas", junto a Pizarras).

Editor de documentos (doceditor/, GrapesJS): no guarda nada propio. Desde el
navegador se ve o descarga el PDF; quien puede subir documentos (admin,
superadmin) lo manda a /documents/upload, que genera el PDF con WeasyPrint.
El widget de MAX de esa página habla con /tools/editor/max (app/tools/doc_assistant.py).
"""
import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from app.auth.deps import require_auth
from app.database import get_db
from app.documents.router import _upload_context
from app.models import DOC_KIND_ANNOUNCEMENT, DOC_KIND_DOCUMENT, ROLE_ADMIN, ROLE_SUPERADMIN
from app.templating import templates
from app.tools import doc_assistant

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("/editor", response_class=HTMLResponse)
def document_editor(
    request: Request, kind: str = DOC_KIND_DOCUMENT, db: Session = Depends(get_db), user=Depends(require_auth),
):
    """`kind`: tipo preseleccionado al subir (?kind=announcement). Desde el editor solo
    se sube como documento o comunicado, nunca como documento de trabajo."""
    can_upload = user.role in (ROLE_ADMIN, ROLE_SUPERADMIN)
    context = _upload_context(user, db) if can_upload else {"current_user": user}
    kind = DOC_KIND_ANNOUNCEMENT if kind == DOC_KIND_ANNOUNCEMENT else DOC_KIND_DOCUMENT
    return templates.TemplateResponse(request, "tools/editor.html", {**context, "can_upload": can_upload, "kind": kind, "allow_work_doc": False,
        "max_widget": {
            "endpoint": "/tools/editor/max", "clear_url": "/tools/editor/max/clear",
            "storage": "doceditor", "fab": False,
            "subtitle": "Redacta o edita este documento",
            "empty_title": "¿Qué escribimos?",
            "empty_text": "Pídeme un documento nuevo o cambios al actual: «redacta una política de vacaciones», «hazlo más formal», «agrega una tabla con…»",
            "placeholder": "Pide un documento o un cambio...",
        },
    })


@router.post("/editor/max", response_class=HTMLResponse)
def document_editor_max(
    request: Request, question: str = Form(...), html: str = Form(default=""), user=Depends(require_auth),
):
    """Un turno del MAX del editor: devuelve las burbujas del widget y, si cambió
    el documento, el HTML nuevo (limpio) para que el editor lo cargue."""
    question = question.strip()
    if not question:
        return HTMLResponse("")
    result = doc_assistant.answer(user, question[:2000], html)
    return templates.TemplateResponse(request, "tools/_max_response.html", {
        "current_user": user, "question": question, "answer": result["answer"],
        "sources": [], "proposals": [], "doc_html": result["html"], "update_id": uuid.uuid4().hex[:12],
    })


@router.post("/editor/max/clear")
def document_editor_max_clear(user=Depends(require_auth)):
    doc_assistant.clear_history(user.id)
    return Response(status_code=204)
