import html as html_lib
import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_auth, require_role
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app.models import Department, Document, ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_SUPERADMIN, STATUSES
from app.permissions import (
    build_access_filter,
    can_access_document,
    can_manage_document,
    can_manage_doc_dict,
)
from app import rag, storage, audit
from app.templating import templates

router = APIRouter(prefix="/documents", tags=["documents"])


# ── Rich-text (TipTap) helpers ────────────────────────────────────────────────

def _safe_slug(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:80] or "documento"


def _html_to_text(fragment: str) -> str:
    """Rough tag-strip, only used to detect empty content."""
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def _wrap_document_html(title: str, fragment: str) -> str:
    """Wrap a TipTap fragment into a standalone, viewable HTML document."""
    safe_title = html_lib.escape(title or "Documento")
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>{safe_title}</title>"
        "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;"
        "margin:2.5rem auto;padding:0 1.25rem;color:#1f2937;line-height:1.6}"
        "h1{font-size:1.6rem;margin:.8em 0 .4em}h2{font-size:1.3rem;margin:.8em 0 .3em}"
        "h3{font-size:1.1rem;margin:.7em 0 .3em}ul{padding-left:1.5rem;list-style:disc}"
        "ol{padding-left:1.5rem;list-style:decimal}blockquote{border-left:3px solid #e5e7eb;"
        "padding-left:.75rem;color:#6b7280;margin:.6em 0}pre{background:#0f172a;color:#e2e8f0;"
        "padding:.75rem;border-radius:.5rem;overflow:auto}code{font-family:ui-monospace,monospace}"
        "a{color:#4f46e5}img{max-width:100%}hr{border:none;border-top:1px solid #e5e7eb;margin:1.2em 0}</style>"
        f"</head><body><h1>{safe_title}</h1>{fragment}</body></html>"
    )


@router.get("/", response_class=HTMLResponse)
def list_documents(
    request: Request,
    q: str = "",
    department_id: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    access_filter = build_access_filter(user)
    raw_docs = rag.search_documents(q, access_filter, department_id or None)

    docs = [
        {**d, "can_manage": can_manage_doc_dict(user, d)}
        for d in raw_docs
    ]

    departments = db.query(Department).order_by(Department.name).all()
    csrf = generate_csrf_token(str(user.id)) if user else ""

    if q:
        audit.log_search(q, user=user, result_count=len(docs), search_type="document")

    return templates.TemplateResponse(
        request, "documents/list.html",
        {
            "documents": docs,
            "departments": departments,
            "current_user": user,
            "query": q,
            "selected_dept": department_id,
            "csrf_token": csrf,
        },
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_form(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    all_depts = db.query(Department).order_by(Department.name).all()

    if user.role == ROLE_SUPERADMIN:
        available_depts = all_depts
        available_statuses = STATUSES
    else:
        available_depts = [d for d in all_depts if str(d.id) == str(user.department_id)]
        available_statuses = STATUSES

    csrf = generate_csrf_token(str(user.id))
    return templates.TemplateResponse(
        request, "documents/upload.html",
        {
            "departments": available_depts,
            "statuses": available_statuses,
            "current_user": user,
            "csrf_token": csrf,
        },
    )


@router.post("/upload")
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    description: str = Form(default=""),
    department_id: str = Form(...),
    status: str = Form(...),
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    if status not in STATUSES:
        raise HTTPException(400, f"Status must be one of: {STATUSES}")
    if user.role == ROLE_ADMIN and str(department_id) != str(user.department_id):
        raise HTTPException(403, "Can only upload to your own department")

    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(404, "Department not found")

    content = await file.read()
    doc_id = str(uuid.uuid4())
    file_key = f"{dept.slug}/{doc_id}/{file.filename}"

    storage.upload_file(file_key, content, file.content_type or "application/octet-stream")
    text = rag.extract_text(content, file.filename or "file")

    doc = Document(
        id=doc_id,
        title=title,
        description=description,
        filename=file.filename or "file",
        file_key=file_key,
        content_type=file.content_type,
        file_size=len(content),
        department_id=department_id,
        status=status,
        uploaded_by=str(user.id),
    )
    db.add(doc)
    db.commit()

    background_tasks.add_task(
        rag.index_document,
        doc_id=doc_id,
        title=title,
        description=description,
        department_id=str(department_id),
        department_name=dept.name,
        status=status,
        content_type=file.content_type or "",
        uploaded_by=str(user.id),
        text=text,
    )

    audit.log_action(
        "upload_document", user=user, request=request,
        resource_type="document", resource_id=doc_id, resource_name=title,
    )
    return RedirectResponse("/documents/", status_code=302)


# ── Create (rich text / TipTap) ───────────────────────────────────────────────

@router.get("/create", response_class=HTMLResponse)
def create_form(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    all_depts = db.query(Department).order_by(Department.name).all()
    if user.role == ROLE_SUPERADMIN:
        available_depts = all_depts
    else:
        available_depts = [d for d in all_depts if str(d.id) == str(user.department_id)]

    return templates.TemplateResponse(
        request, "documents/editor.html",
        {
            "mode": "create",
            "action": "/documents/create",
            "doc": None,
            "departments": available_depts,
            "statuses": STATUSES,
            "current_user": user,
            "csrf_token": generate_csrf_token(str(user.id)),
        },
    )


@router.post("/create")
def create_document(
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    description: str = Form(default=""),
    department_id: str = Form(...),
    status: str = Form(...),
    body: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    if status not in STATUSES:
        raise HTTPException(400, f"Status must be one of: {STATUSES}")
    if user.role == ROLE_ADMIN and str(department_id) != str(user.department_id):
        raise HTTPException(403, "Can only create in your own department")

    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(404, "Department not found")

    fragment = body.strip()
    if not _html_to_text(fragment):
        raise HTTPException(400, "El documento está vacío")

    doc_id = str(uuid.uuid4())
    filename = f"{_safe_slug(title)}.html"
    file_key = f"{dept.slug}/{doc_id}/{filename}"
    full_html = _wrap_document_html(title, fragment)
    html_bytes = full_html.encode("utf-8")

    storage.upload_file(file_key, html_bytes, "text/html")
    text = rag.extract_text(html_bytes, "documento.html") or _html_to_text(fragment)

    doc = Document(
        id=doc_id,
        title=title,
        description=description,
        filename=filename,
        file_key=file_key,
        content_type="text/html",
        file_size=len(html_bytes),
        content_html=fragment,
        department_id=department_id,
        status=status,
        uploaded_by=str(user.id),
    )
    db.add(doc)
    db.commit()

    background_tasks.add_task(
        rag.index_document,
        doc_id=doc_id,
        title=title,
        description=description,
        department_id=str(department_id),
        department_name=dept.name,
        status=status,
        content_type="text/html",
        uploaded_by=str(user.id),
        text=text,
    )
    audit.log_action(
        "create_document", user=user, request=request,
        resource_type="document", resource_id=doc_id, resource_name=title,
    )
    return RedirectResponse("/documents/", status_code=302)


@router.get("/{doc_id}/edit", response_class=HTMLResponse)
def edit_form(
    doc_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404)
    if not can_manage_document(user, doc):
        raise HTTPException(403)

    depts = db.query(Department).order_by(Department.name).all()
    csrf = generate_csrf_token(str(user.id))

    # Rich-text docs (authored in-app) reopen in the TipTap editor.
    if doc.content_html is not None:
        return templates.TemplateResponse(
            request, "documents/editor.html",
            {
                "mode": "edit",
                "action": f"/documents/{doc.id}/edit",
                "doc": doc,
                "departments": depts,
                "statuses": STATUSES,
                "current_user": user,
                "csrf_token": csrf,
            },
        )

    return templates.TemplateResponse(
        request, "documents/edit.html",
        {
            "doc": doc,
            "departments": depts,
            "statuses": STATUSES,
            "current_user": user,
            "csrf_token": csrf,
        },
    )


@router.post("/{doc_id}/edit")
async def edit_document(
    doc_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    description: str = Form(default=""),
    status: str = Form(...),
    csrf_token: str = Form(...),
    body: str = Form(default=""),
    file: UploadFile = File(default=None),
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404)
    if not can_manage_document(user, doc):
        raise HTTPException(403)
    if status not in STATUSES:
        raise HTTPException(400)

    doc.title = title
    doc.description = description
    doc.status = status

    dept = db.query(Department).filter(Department.id == doc.department_id).first()
    new_text: str | None = None

    if doc.content_html is not None:
        # Rich-text doc edited in TipTap — regenerate the stored HTML in place.
        fragment = body.strip()
        if not _html_to_text(fragment):
            raise HTTPException(400, "El documento está vacío")
        full_html = _wrap_document_html(title, fragment)
        html_bytes = full_html.encode("utf-8")
        storage.upload_file(doc.file_key, html_bytes, "text/html")  # overwrite same key
        doc.content_html = fragment
        doc.content_type = "text/html"
        doc.filename = f"{_safe_slug(title)}.html"
        doc.file_size = len(html_bytes)
        new_text = rag.extract_text(html_bytes, "documento.html") or _html_to_text(fragment)
    elif file and file.filename:
        content = await file.read()
        if content:
            new_file_key = f"{dept.slug if dept else 'misc'}/{doc_id}/{file.filename}"
            storage.delete_file(doc.file_key)
            storage.upload_file(new_file_key, content, file.content_type or "application/octet-stream")
            doc.file_key = new_file_key
            doc.filename = file.filename
            doc.content_type = file.content_type
            doc.file_size = len(content)
            new_text = rag.extract_text(content, file.filename)

    db.commit()

    if new_text is not None:
        # New file uploaded — delete old chunks and reindex with new content
        rag.delete_document_from_index(doc_id)
        background_tasks.add_task(
            rag.index_document,
            doc_id=doc_id,
            title=doc.title,
            description=doc.description,
            department_id=str(doc.department_id),
            department_name=dept.name if dept else "",
            status=doc.status,
            content_type=doc.content_type or "",
            uploaded_by=str(doc.uploaded_by),
            text=new_text,
        )
    else:
        # Metadata only — update title/status/dept in existing chunks
        background_tasks.add_task(
            rag.update_document_metadata,
            doc_id=doc_id,
            title=doc.title,
            description=doc.description,
            department_id=str(doc.department_id),
            department_name=dept.name if dept else "",
            status=doc.status,
            content_type=doc.content_type or "",
            uploaded_by=str(doc.uploaded_by),
        )

    audit.log_action(
        "edit_document", user=user, request=request,
        resource_type="document", resource_id=doc_id, resource_name=title,
    )
    return RedirectResponse("/documents/", status_code=302)


@router.get("/{doc_id}/view")
def view_document(
    doc_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    if not can_access_document(user, doc):
        raise HTTPException(403 if user else 401, "Access denied")

    return RedirectResponse(storage.get_signed_url(doc.file_key))


@router.get("/{doc_id}/download")
def download_document(
    doc_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    if not can_access_document(user, doc):
        raise HTTPException(403 if user else 401, "Access denied")

    audit.log_action(
        "download_document", user=user,
        resource_type="document", resource_id=doc_id, resource_name=doc.title,
    )
    return RedirectResponse(storage.get_signed_url(doc.file_key, filename=doc.filename))


@router.post("/{doc_id}/delete")
def delete_document(
    doc_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Document not found")
    if not can_manage_document(user, doc):
        raise HTTPException(403, "Access denied")

    title = doc.title
    storage.delete_file(doc.file_key)
    rag.delete_document_from_index(doc_id)
    db.delete(doc)
    db.commit()

    audit.log_action(
        "delete_document", user=user, request=request,
        resource_type="document", resource_id=doc_id, resource_name=title,
    )
    return RedirectResponse("/documents/", status_code=302)
