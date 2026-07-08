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

    if file and file.filename:
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

    audit.log_action(
        "view_document", user=user,
        resource_type="document", resource_id=doc_id, resource_name=doc.title,
    )
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
