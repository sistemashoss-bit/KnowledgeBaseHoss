"""Herramientas: utilidades de uso general (menú "Herramientas", junto a Pizarras).

Editor de documentos (doceditor/, GrapesJS): no guarda nada propio. Desde el
navegador se ve o descarga el PDF; quien puede subir documentos (admin,
superadmin) lo manda a /documents/upload, que genera el PDF con WeasyPrint.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_auth
from app.database import get_db
from app.documents.router import _upload_context
from app.models import ROLE_ADMIN, ROLE_SUPERADMIN
from app.templating import templates

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("/editor", response_class=HTMLResponse)
def document_editor(request: Request, db: Session = Depends(get_db), user=Depends(require_auth)):
    can_upload = user.role in (ROLE_ADMIN, ROLE_SUPERADMIN)
    context = _upload_context(user, db) if can_upload else {"current_user": user}
    return templates.TemplateResponse(request, "tools/editor.html", {**context, "can_upload": can_upload})
