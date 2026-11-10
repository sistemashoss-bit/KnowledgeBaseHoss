"""Comunicados: documentos con kind="announcement" (misma tabla, permisos,
índice y acciones que Documentos; ver app/documents/router.py). Solo PDF.
Aquí viven el listado, el alta y la confirmación de visto (reads.py): el
botón "Enterado" y la página de quién lo vio. Ver, descargar, compartir,
editar y borrar usan las rutas de /documents/.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import audit, rag
from app.announcements import reads
from app.auth.deps import get_current_user, require_auth, require_role
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app.documents.router import _preview_meta, _upload_context
from app.models import DOC_KIND_ANNOUNCEMENT, Document, ROLE_ADMIN, ROLE_SUPERADMIN, User
from app.permissions import build_access_filter, can_access_document, can_manage_doc_dict, can_manage_document
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
        str(d.id): (d.created_at, name or email, d)
        for d, name, email in (
            db.query(Document, User.name, User.email)
            .outerjoin(User, User.id == Document.uploaded_by)
            .filter(Document.id.in_(ids), Document.kind == DOC_KIND_ANNOUNCEMENT)
            .all()
        )
    } if ids else {}
    mine = reads.my_reads(db, user, list(rows))
    docs = []
    for h in hits:
        row = rows.get(str(h.get("id")))
        if not row:
            continue  # índice desfasado (borrado o cambió de tipo)
        doc = row[2]
        can_manage = can_manage_doc_dict(user, h)
        read = mine.get(str(doc.id))
        docs.append({
            **h,
            "published_at": row[0],
            "author": row[1],
            "can_manage": can_manage,
            "announcement": {
                "requires_ack": doc.requires_ack,
                "recipient": reads.is_recipient(doc, user),
                "seen": bool(read and read.seen_at),
                "acknowledged_at": read.acknowledged_at if read else None,
                # Conteo para quien lo publica o administra.
                "stats": reads.stats(db, doc) if can_manage else None,
            },
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


def _announcement_or_404(db: Session, doc_id: str) -> Document:
    try:
        uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(404, "Comunicado no encontrado")
    doc = db.query(Document).filter(Document.id == doc_id, Document.kind == DOC_KIND_ANNOUNCEMENT).first()
    if not doc:
        raise HTTPException(404, "Comunicado no encontrado")
    return doc


@router.post("/{doc_id}/ack")
def acknowledge_announcement(doc_id: str, db: Session = Depends(get_db), user=Depends(require_auth)):
    """Botón "Enterado": constancia de que la persona leyó el comunicado."""
    doc = _announcement_or_404(db, doc_id)
    if not can_access_document(user, doc):
        raise HTTPException(403)
    if not doc.requires_ack or not reads.is_recipient(doc, user):
        raise HTTPException(400, "Este comunicado no pide confirmación.")
    reads.acknowledge(db, doc, user)
    audit.log_action(
        "acknowledge_announcement", user=user,
        resource_type="document", resource_id=str(doc.id), resource_name=doc.title,
    )
    return RedirectResponse(f"/announcements/#a-{doc.id}", status_code=303)


@router.get("/{doc_id}/reads", response_class=HTMLResponse)
def announcement_reads(request: Request, doc_id: str, db: Session = Depends(get_db), user=Depends(require_auth)):
    """Quién lo vio y quién confirmó: para quien puede administrar el comunicado."""
    doc = _announcement_or_404(db, doc_id)
    if not can_manage_document(user, doc):
        raise HTTPException(403)
    people = reads.detail(db, doc)
    return templates.TemplateResponse(request, "announcements/reads.html", {
        "current_user": user,
        "doc": doc,
        "people": people,
        "stats": reads.stats(db, doc),
    })
