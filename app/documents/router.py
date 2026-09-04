import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_auth, require_role
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app.models import Department, Document, ROLE_ADMIN, ROLE_SUPERADMIN, STATUSES
from app.permissions import (
    build_access_filter,
    can_access_document,
    can_manage_document,
    can_manage_doc_dict,
)
from app import rag, storage, audit
from app.templating import templates

router = APIRouter(prefix="/documents", tags=["documents"])


# ── Google Drive helpers ──────────────────────────────────────────────────────

_PREVIEWABLE_CONTENT_TYPES = ("application/pdf", "text/html")


def is_drive_url(url: str) -> bool:
    """Acepta enlaces de Google Drive / Docs / Sheets / Slides."""
    return bool(re.search(r"https?://(drive|docs)\.google\.com/", (url or "").strip()))


def drive_embed_url(url: str) -> str:
    """Convierte un link de Drive/Docs a su URL embebible en iframe (/preview)."""
    url = (url or "").strip()
    m = re.search(r"/file/d/([^/]+)", url)
    if m:
        return f"https://drive.google.com/file/d/{m.group(1)}/preview"
    m = re.search(r"(document|spreadsheets|presentation)/d/([^/]+)", url)
    if m:
        return f"https://docs.google.com/{m.group(1)}/d/{m.group(2)}/preview"
    m = re.search(r"/folders/([^/?]+)", url)
    if m:
        return f"https://drive.google.com/embeddedfolderview?id={m.group(1)}"
    m = re.search(r"[?&]id=([^&]+)", url)
    if m:
        return f"https://drive.google.com/file/d/{m.group(1)}/preview"
    return url


def _preview_meta(doc_id: str, content_type: str, drive_url: str | None) -> dict:
    """Metadatos de previsualización para la card: url del iframe + si aplica."""
    if drive_url:
        return {
            "previewable": True,
            "is_drive": True,
            "preview_url": drive_embed_url(drive_url),
            "external_url": drive_url,
        }
    ct = (content_type or "").lower()
    if ct in _PREVIEWABLE_CONTENT_TYPES or ct.startswith("image/"):
        # /view redirige a la URL firmada; el navegador renderiza PDF/imagen/HTML.
        return {
            "previewable": True,
            "is_drive": False,
            "preview_url": f"/documents/{doc_id}/view",
            "external_url": None,
        }
    return {"previewable": False, "is_drive": False, "preview_url": None, "external_url": None}


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

    # OpenSearch no guarda drive_url; se enriquece desde la BD por id.
    ids = [d["id"] for d in raw_docs if d.get("id")]
    drive_map: dict[str, str | None] = {}
    if ids:
        for row in db.query(Document.id, Document.drive_url).filter(Document.id.in_(ids)).all():
            drive_map[str(row.id)] = row.drive_url

    docs = []
    for d in raw_docs:
        drive_url = drive_map.get(str(d.get("id")))
        docs.append({
            **d,
            "can_manage": can_manage_doc_dict(user, d),
            **_preview_meta(d.get("id"), d.get("content_type", ""), drive_url),
        })

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
    drive_url: str = Form(default=""),
    file: UploadFile = File(default=None),
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

    # Un documento es un archivo subido O un link de Drive, no ambos ni ninguno.
    drive_url = drive_url.strip()
    has_file = bool(file and file.filename)
    if has_file == bool(drive_url):
        raise HTTPException(400, "Proporciona un archivo o un link de Google Drive (uno de los dos).")

    doc_id = str(uuid.uuid4())

    if drive_url:
        if not is_drive_url(drive_url):
            raise HTTPException(400, "El link no parece de Google Drive/Docs.")
        doc = Document(
            id=doc_id,
            title=title,
            description=description,
            filename=None,
            file_key=None,
            content_type="drive",
            file_size=None,
            drive_url=drive_url,
            department_id=department_id,
            status=status,
            uploaded_by=str(user.id),
        )
        content_type_for_index = "drive"
        text = ""  # sin archivo: indexable solo por título/descripción
    else:
        content = await file.read()
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
        content_type_for_index = file.content_type or ""

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
        content_type=content_type_for_index,
        uploaded_by=str(user.id),
        text=text,
    )

    audit.log_action(
        "upload_document", user=user, request=request,
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
    drive_url: str = Form(default=""),
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
    drive_url = drive_url.strip()

    if doc.drive_url is not None:
        # Documento de Drive: solo se puede actualizar el link (no hay archivo).
        if drive_url:
            if not is_drive_url(drive_url):
                raise HTTPException(400, "El link no parece de Google Drive/Docs.")
            doc.drive_url = drive_url
    elif file and file.filename:
        content = await file.read()
        if content:
            new_file_key = f"{dept.slug if dept else 'misc'}/{doc_id}/{file.filename}"
            if doc.file_key:
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

    if doc.drive_url:
        return RedirectResponse(doc.drive_url)
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

    if doc.drive_url:
        # Docs de Drive no se descargan localmente: se abre el link.
        return RedirectResponse(doc.drive_url)

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
    if doc.file_key:  # docs de Drive no tienen archivo en Wasabi
        storage.delete_file(doc.file_key)
    rag.delete_document_from_index(doc_id)
    db.delete(doc)
    db.commit()

    audit.log_action(
        "delete_document", user=user, request=request,
        resource_type="document", resource_id=doc_id, resource_name=title,
    )
    return RedirectResponse("/documents/", status_code=302)
