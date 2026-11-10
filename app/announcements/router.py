"""Comunicados: documentos con kind="announcement" (misma tabla, permisos,
índice y acciones que Documentos; ver app/documents/router.py). Solo PDF.
Aquí solo viven el listado y el alta; ver, descargar, compartir, editar y
borrar usan las rutas de /documents/.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import audit, rag
from app.auth.deps import get_current_user, require_role
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app.documents.router import _preview_meta, _upload_context
from app.models import DOC_KIND_ANNOUNCEMENT, Document, ROLE_ADMIN, ROLE_SUPERADMIN, User
from app.permissions import build_access_filter, can_manage_doc_dict
from app.templating import templates

router = APIRouter(prefix="/announcements", tags=["announcements"])


@router.get("/", response_class=HTMLResponse)
def list_announcements(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    # Igual que /documents/: sin sesión se ven solo los públicos.
    hits = rag.search_documents(q, build_access_filter(user), size=200, kind=DOC_KIND_ANNOUNCEMENT)
    # OpenSearch no guarda la fecha: se toma de la BD (con el autor) y se
    # ordena del más reciente al más antiguo; sin búsqueda manda la fecha,
    # con búsqueda también (es una lista de avisos, no de relevancia).
    ids = [h["id"] for h in hits if h.get("id")]
    rows = {
        str(d.id): (d.created_at, name or email)
        for d, name, email in (
            db.query(Document, User.name, User.email)
            .outerjoin(User, User.id == Document.uploaded_by)
            .filter(Document.id.in_(ids), Document.kind == DOC_KIND_ANNOUNCEMENT)
            .all()
        )
    } if ids else {}
    docs = []
    for h in hits:
        row = rows.get(str(h.get("id")))
        if not row:
            continue  # índice desfasado (borrado o cambió de tipo)
        docs.append({
            **h,
            "published_at": row[0],
            "author": row[1],
            "can_manage": can_manage_doc_dict(user, h),
            **_preview_meta(h.get("id"), h.get("content_type", ""), None),
        })
    docs.sort(key=lambda d: d["published_at"] or 0, reverse=True)

    if q:
        audit.log_search(q, user=user, result_count=len(docs), search_type="announcement")

    return templates.TemplateResponse(request, "announcements/list.html", {
        "documents": docs,
        "query": q,
        "current_user": user,
        "csrf_token": generate_csrf_token(str(user.id)) if user else "",
        "can_publish": bool(user) and user.role in (ROLE_ADMIN, ROLE_SUPERADMIN),
    })


@router.get("/new", response_class=HTMLResponse)
def new_announcement(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    return templates.TemplateResponse(
        request, "documents/upload.html", {**_upload_context(user, db), "kind": DOC_KIND_ANNOUNCEMENT},
    )
