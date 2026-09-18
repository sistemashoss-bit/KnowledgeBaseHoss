import re
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_auth, require_role
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app.models import (
    Branch, Department, Document, DocumentAllowedUser, Folder, FolderAllowedUser, User, Zone,
    FOLDER_MAX_DEPTH, ROLE_ADMIN, ROLE_SUPERADMIN, STATUSES, STATUS_CUSTOM,
)
from app.permissions import (
    build_access_filter,
    build_shared_with_me_filter,
    can_access_document,
    can_access_folder,
    can_manage_document,
    can_manage_doc_dict,
    can_manage_folder,
)
from app import folders as folders_lib
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


def _own_folders(db: Session, owner_id) -> list[Folder]:
    """Todas las carpetas del owner, en orden de árbol (padres antes que
    hijos) para mostrarlas indentadas por profundidad en un <select> plano."""
    all_folders = db.query(Folder).filter(Folder.owner_id == owner_id).all()
    by_parent: dict[str | None, list[Folder]] = {}
    for f in all_folders:
        by_parent.setdefault(str(f.parent_id) if f.parent_id else None, []).append(f)

    ordered: list[Folder] = []

    def walk(parent_key: str | None) -> None:
        for f in sorted(by_parent.get(parent_key, []), key=lambda x: x.name):
            ordered.append(f)
            walk(str(f.id))

    walk(None)
    return ordered


def _reindex_document_access(db: Session, doc: Document, background_tasks: BackgroundTasks) -> None:
    """Recalcula allowed_user_ids/folder_shared_user_ids desde la BD y
    actualiza el índice. Se usa tras compartir/revocar (doc o carpeta)."""
    dept = db.query(Department).filter(Department.id == doc.department_id).first()
    allowed_user_ids = [
        str(au.user_id) for au in db.query(DocumentAllowedUser).filter(
            DocumentAllowedUser.document_id == doc.id
        ).all()
    ]
    folder_shared_user_ids = (
        sorted(folders_lib.effective_shared_user_ids(db, doc.folder_id)) if doc.folder_id else []
    )
    background_tasks.add_task(
        rag.update_document_metadata,
        doc_id=str(doc.id),
        title=doc.title,
        description=doc.description,
        department_id=str(doc.department_id),
        department_name=dept.name if dept else "",
        status=doc.status,
        content_type=doc.content_type or "",
        uploaded_by=str(doc.uploaded_by),
        allowed_user_ids=allowed_user_ids,
        folder_shared_user_ids=folder_shared_user_ids,
        is_work_document=doc.is_work_document,
    )


@router.get("/", response_class=HTMLResponse)
def list_documents(
    request: Request,
    q: str = "",
    department_id: str = "",
    view: str = "",
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
    selected_dept_name = ""
    if department_id:
        selected_dept_obj = db.query(Department).filter(Department.id == department_id).first()
        selected_dept_name = selected_dept_obj.name if selected_dept_obj else ""
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
            "selected_dept_name": selected_dept_name,
            "view_mode": view,
            "csrf_token": csrf,
        },
    )


@router.get("/mine", response_class=HTMLResponse)
def list_my_documents(
    request: Request,
    q: str = "",
    department_id: str = "",
    zone_id: str = "",
    employee_id: str = "",
    tab: str = "shared",
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    """Mis documentos, en dos pestañas: "compartidos" son solo los que me
    compartieron directamente (status='custom' + estoy en allowed_user_ids),
    no todo lo que puedo ver por visibilidad general (público/empleados/etc.,
    eso ya está en /documents/). "Trabajo" sigue usando la visibilidad normal
    (build_access_filter) filtrada por `is_work_document`; en vez del filtro
    de departamento (redundante: el admin solo tiene el suyo), el admin puede
    filtrar por empleado de su depto (o, si su depto pertenece a una zona,
    primero por zona y luego por empleado dentro de esa zona)."""
    if tab not in ("shared", "work"):
        tab = "shared"

    departments = db.query(Department).order_by(Department.name).all()

    zones: list[Zone] = []
    employees: list[User] = []
    selected_zone_value = ""
    show_employee_filter = tab == "work" and user.role == ROLE_ADMIN

    if show_employee_filter:
        dept_zone = (
            user.department.branch.zone
            if user.department and user.department.branch and user.department.branch.zone_id
            else None
        )
        if dept_zone:
            zones = db.query(Zone).order_by(Zone.name).all()
            if not zone_id:
                effective_zone_id: str | None = str(dept_zone.id)
                selected_zone_value = effective_zone_id
            elif zone_id == "all":
                effective_zone_id = None
                selected_zone_value = "all"
            else:
                effective_zone_id = zone_id
                selected_zone_value = zone_id

            employees_query = db.query(User).filter(User.is_active == True)  # noqa: E712
            if effective_zone_id:
                employees_query = employees_query.join(Department, User.department_id == Department.id).join(
                    Branch, Department.branch_id == Branch.id
                ).filter(Branch.zone_id == effective_zone_id)
            employees = employees_query.order_by(User.name, User.email).all()
        else:
            employees = (
                db.query(User)
                .filter(User.is_active == True, User.department_id == user.department_id)  # noqa: E712
                .order_by(User.name, User.email)
                .all()
            )

    if tab == "work":
        access_filter = build_access_filter(user)
        search_department_id = None
        search_uploaded_by = employee_id or None
    else:
        access_filter = build_shared_with_me_filter(user)
        search_department_id = department_id or None
        search_uploaded_by = None

    raw_docs = rag.search_documents(
        q, access_filter, search_department_id, size=200,
        work_only=(tab == "work"), uploaded_by=search_uploaded_by,
    )

    ids = [d["id"] for d in raw_docs if d.get("id")]
    drive_map: dict[str, str | None] = {}
    if ids:
        for row in db.query(Document.id, Document.drive_url).filter(Document.id.in_(ids)).all():
            drive_map[str(row.id)] = row.drive_url

    can_drag_to_folder = tab == "work" and user.role == ROLE_ADMIN

    docs = []
    for d in raw_docs:
        drive_url = drive_map.get(str(d.get("id")))
        docs.append({
            **d,
            "can_manage": can_manage_doc_dict(user, d),
            "can_assign_folder": can_drag_to_folder and d.get("uploaded_by") == str(user.id),
            **_preview_meta(d.get("id"), d.get("content_type", ""), drive_url),
        })

    own_folders = _own_folders(db, user.id) if can_drag_to_folder else []

    csrf = generate_csrf_token(str(user.id))

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
            "list_action": "/documents/mine",
            "page_title": "Mis documentos",
            "empty_message": "No hay documentos de trabajo" if tab == "work" else "No tienes documentos compartidos",
            "mine_tab": tab,
            "show_employee_filter": show_employee_filter,
            "zones": zones,
            "employees": employees,
            "selected_zone": selected_zone_value,
            "selected_employee": employee_id,
            "folders": own_folders,
        },
    )


@router.get("/upload", response_class=HTMLResponse)
def upload_form(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    available_statuses = STATUSES

    if user.role == ROLE_SUPERADMIN:
        # Superadmin elige cualquier departamento; el picker de "personas
        # específicas" se filtra en el cliente por departamento (mismo patrón
        # que tasks/list.html con data-dept) porque aún no se sabe cuál elegirá.
        available_depts = db.query(Department).order_by(Department.name).all()
        members = (
            db.query(User)
            .filter(User.is_active == True)  # noqa: E712
            .order_by(User.name, User.email)
            .all()
        )
    else:
        # Admin: el documento solo puede quedar en su propio departamento.
        available_depts = [user.department] if user.department else []
        members = (
            db.query(User)
            .filter(User.is_active == True, User.department_id == user.department_id)  # noqa: E712
            .order_by(User.name, User.email)
            .all()
        )

    own_folders = _own_folders(db, user.id) if user.role == ROLE_ADMIN else []

    csrf = generate_csrf_token(str(user.id))
    return templates.TemplateResponse(
        request, "documents/upload.html",
        {
            "departments": available_depts,
            "statuses": available_statuses,
            "members": members,
            "folders": own_folders,
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
    is_work_document: bool = Form(default=False),
    folder_id: str = Form(default=""),
    allowed_user_ids: list[str] = Form(default=[]),
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

    if user.role == ROLE_ADMIN:
        # Un admin solo puede crear documentos en su propio departamento,
        # sin importar lo que venga en el form (por si lo manipulan).
        department_id = str(user.department_id) if user.department_id else ""

    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(404, "Department not found")

    # Carpetas: solo admin, solo docs de trabajo, solo carpetas propias.
    folder_id = folder_id.strip()
    folder: Folder | None = None
    if folder_id:
        if user.role != ROLE_ADMIN or not is_work_document:
            raise HTTPException(400, "Las carpetas son solo para documentos de trabajo del admin dueño de la carpeta.")
        folder = db.query(Folder).filter(Folder.id == folder_id).first()
        if not folder or str(folder.owner_id) != str(user.id):
            raise HTTPException(404, "Carpeta no encontrada.")

    allowed_user_ids = [uid for uid in dict.fromkeys(allowed_user_ids) if uid]
    if status == STATUS_CUSTOM:
        if not allowed_user_ids:
            raise HTTPException(400, "Selecciona al menos una persona para visibilidad 'personas específicas'.")
        # Al crear, solo personas del departamento del documento; para dar
        # acceso a alguien de otro depto se usa "compartir" después de crearlo.
        valid_ids = {
            str(u.id) for u in db.query(User.id).filter(
                User.is_active == True, User.department_id == department_id,  # noqa: E712
                User.id.in_(allowed_user_ids),
            ).all()
        }
        if not all(uid in valid_ids for uid in allowed_user_ids):
            raise HTTPException(400, "Alguna de las personas seleccionadas no existe, está inactiva o no pertenece al departamento del documento.")
    else:
        allowed_user_ids = []

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
            is_work_document=is_work_document,
            uploaded_by=str(user.id),
            folder_id=folder.id if folder else None,
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
            is_work_document=is_work_document,
            uploaded_by=str(user.id),
            folder_id=folder.id if folder else None,
        )
        content_type_for_index = file.content_type or ""

    db.add(doc)
    for uid in allowed_user_ids:
        db.add(DocumentAllowedUser(document_id=doc_id, user_id=uid))
    db.commit()

    folder_shared_user_ids = sorted(folders_lib.effective_shared_user_ids(db, folder.id)) if folder else []

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
        allowed_user_ids=allowed_user_ids,
        is_work_document=is_work_document,
        folder_shared_user_ids=folder_shared_user_ids,
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

    # El departamento del documento no se puede cambiar al editar, así que el
    # picker de "personas específicas" solo ofrece gente de ese mismo depto.
    # Para dar acceso a alguien de otro departamento se usa "Compartir".
    members = (
        db.query(User)
        .filter(User.is_active == True, User.department_id == doc.department_id)  # noqa: E712
        .order_by(User.name, User.email)
        .all()
    )
    selected_user_ids = {str(au.user_id) for au in doc.allowed_users}

    # Carpetas: solo el propio admin que subió el documento, y solo si es de trabajo.
    can_assign_folder = (
        user.role == ROLE_ADMIN and doc.is_work_document and str(doc.uploaded_by) == str(user.id)
    )
    own_folders = _own_folders(db, user.id) if can_assign_folder else []

    csrf = generate_csrf_token(str(user.id))

    return templates.TemplateResponse(
        request, "documents/edit.html",
        {
            "doc": doc,
            "statuses": STATUSES,
            "members": members,
            "selected_user_ids": selected_user_ids,
            "can_assign_folder": can_assign_folder,
            "folders": own_folders,
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
    is_work_document: bool = Form(default=False),
    folder_id: str = Form(default=""),
    allowed_user_ids: list[str] = Form(default=[]),
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

    # Solo el propio admin dueño del documento gestiona su carpeta; cualquier
    # otro editor (superadmin, admin del depto gestionando algo ajeno) deja
    # doc.folder_id tal cual estaba, aunque el form no traiga ese campo.
    if user.role == ROLE_ADMIN and str(doc.uploaded_by) == str(user.id):
        folder_id = folder_id.strip()
        folder: Folder | None = None
        if folder_id and is_work_document:
            folder = db.query(Folder).filter(Folder.id == folder_id).first()
            if not folder or str(folder.owner_id) != str(user.id):
                raise HTTPException(404, "Carpeta no encontrada.")
        doc.folder_id = folder.id if folder else None

    allowed_user_ids = [uid for uid in dict.fromkeys(allowed_user_ids) if uid]
    if status == STATUS_CUSTOM:
        if not allowed_user_ids:
            raise HTTPException(400, "Selecciona al menos una persona para visibilidad 'personas específicas'.")
        # El depto del documento no se puede cambiar al editar: solo gente de ese mismo depto.
        valid_ids = {
            str(u.id) for u in db.query(User.id).filter(
                User.is_active == True, User.department_id == doc.department_id,  # noqa: E712
                User.id.in_(allowed_user_ids),
            ).all()
        }
        if not all(uid in valid_ids for uid in allowed_user_ids):
            raise HTTPException(400, "Alguna de las personas seleccionadas no existe, está inactiva o no pertenece al departamento del documento.")
    else:
        allowed_user_ids = []

    # Este picker solo gestiona accesos dentro del depto del documento; lo
    # compartido con gente de otros deptos (vía "Compartir") no se toca aquí.
    dept_user_ids = {
        str(u.id) for u in db.query(User.id).filter(User.department_id == doc.department_id).all()
    }
    db.query(DocumentAllowedUser).filter(
        DocumentAllowedUser.document_id == doc_id,
        DocumentAllowedUser.user_id.in_(dept_user_ids),
    ).delete(synchronize_session=False)
    for uid in allowed_user_ids:
        db.add(DocumentAllowedUser(document_id=doc_id, user_id=uid))

    doc.title = title
    doc.description = description
    doc.status = status
    doc.is_work_document = is_work_document

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

    # Recalcula el set completo (picker del depto + lo compartido con otros
    # deptos vía "Compartir") para que el índice refleje el acceso real.
    full_allowed_user_ids = [
        str(au.user_id) for au in db.query(DocumentAllowedUser).filter(
            DocumentAllowedUser.document_id == doc_id
        ).all()
    ]
    folder_shared_user_ids = (
        sorted(folders_lib.effective_shared_user_ids(db, doc.folder_id)) if doc.folder_id else []
    )

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
            allowed_user_ids=full_allowed_user_ids,
            is_work_document=doc.is_work_document,
            folder_shared_user_ids=folder_shared_user_ids,
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
            allowed_user_ids=full_allowed_user_ids,
            is_work_document=doc.is_work_document,
            folder_shared_user_ids=folder_shared_user_ids,
        )

    audit.log_action(
        "edit_document", user=user, request=request,
        resource_type="document", resource_id=doc_id, resource_name=title,
    )
    return RedirectResponse("/documents/", status_code=302)


@router.post("/{doc_id}/move-to-folder")
def move_document_to_folder(
    doc_id: str,
    background_tasks: BackgroundTasks,
    folder_id: str = Form(default=""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    """Mueve un documento de trabajo propio a una carpeta (folder_id vacío =
    lo saca de la carpeta). Usado por el drag & drop en la pestaña "trabajo"
    de /documents/mine; misma regla que el selector de carpeta al editar:
    solo el admin dueño del documento, y solo si es de trabajo."""
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(404, "Documento no encontrado.")
    if user.role != ROLE_ADMIN or not doc.is_work_document or str(doc.uploaded_by) != str(user.id):
        raise HTTPException(403, "Solo puedes organizar en carpetas tus propios documentos de trabajo.")

    folder_id = folder_id.strip()
    folder: Folder | None = None
    if folder_id:
        folder = db.query(Folder).filter(Folder.id == folder_id).first()
        if not folder or str(folder.owner_id) != str(user.id):
            raise HTTPException(404, "Carpeta no encontrada.")

    doc.folder_id = folder.id if folder else None
    db.commit()
    _reindex_document_access(db, doc, background_tasks)

    audit.log_action(
        "move_document_to_folder", user=user,
        resource_type="document", resource_id=doc_id, resource_name=doc.title,
        details=f"folder={folder.name if folder else '(ninguna)'}",
    )
    return JSONResponse({"ok": True, "folder_id": str(folder.id) if folder else None})


# ── Compartir (revocable, independiente del departamento/status del doc) ─────

@router.get("/{doc_id}/share", response_class=HTMLResponse)
def share_form(
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

    shared_users = (
        db.query(User)
        .join(DocumentAllowedUser, DocumentAllowedUser.user_id == User.id)
        .filter(DocumentAllowedUser.document_id == doc_id)
        .order_by(User.name, User.email)
        .all()
    )
    shared_ids = {str(u.id) for u in shared_users}

    candidates_query = db.query(User).filter(User.is_active == True)  # noqa: E712
    if shared_ids:
        candidates_query = candidates_query.filter(~User.id.in_(shared_ids))
    candidates = candidates_query.order_by(User.name, User.email).all()

    csrf = generate_csrf_token(str(user.id))
    return templates.TemplateResponse(
        request, "documents/share.html",
        {
            "doc": doc,
            "shared_users": shared_users,
            "candidates": candidates,
            "current_user": user,
            "csrf_token": csrf,
        },
    )


@router.post("/{doc_id}/share")
def share_document(
    doc_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    target_user_id: str = Form(...),
    csrf_token: str = Form(...),
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

    target = db.query(User).filter(User.id == target_user_id, User.is_active == True).first()  # noqa: E712
    if not target:
        raise HTTPException(400, "Persona no encontrada o inactiva.")

    already_shared = db.query(DocumentAllowedUser).filter(
        DocumentAllowedUser.document_id == doc_id,
        DocumentAllowedUser.user_id == target_user_id,
    ).first()
    if not already_shared:
        db.add(DocumentAllowedUser(document_id=doc_id, user_id=target_user_id))
        db.commit()
        _reindex_document_access(db, doc, background_tasks)
        audit.log_action(
            "share_document", user=user, request=request,
            resource_type="document", resource_id=doc_id, resource_name=doc.title,
            details=f"shared_with={target.email}",
        )
    return RedirectResponse(f"/documents/{doc_id}/share", status_code=302)


@router.post("/{doc_id}/share/{target_user_id}/revoke")
def revoke_document_share(
    doc_id: str,
    target_user_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(...),
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

    grant = db.query(DocumentAllowedUser).filter(
        DocumentAllowedUser.document_id == doc_id,
        DocumentAllowedUser.user_id == target_user_id,
    ).first()
    if grant:
        target_email = grant.user.email if grant.user else target_user_id
        db.delete(grant)
        db.commit()
        _reindex_document_access(db, doc, background_tasks)
        audit.log_action(
            "revoke_document_share", user=user, request=request,
            resource_type="document", resource_id=doc_id, resource_name=doc.title,
            details=f"revoked_from={target_email}",
        )
    return RedirectResponse(f"/documents/{doc_id}/share", status_code=302)


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


# ── Carpetas (solo docs de trabajo, hasta FOLDER_MAX_DEPTH niveles) ──────────

@router.get("/folders", response_class=HTMLResponse)
def folders_root(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    own_folders = [f for f in _own_folders(db, user.id) if f.parent_id is None] if user.role == ROLE_ADMIN else []
    shared_folders = (
        db.query(Folder)
        .join(FolderAllowedUser, FolderAllowedUser.folder_id == Folder.id)
        .filter(FolderAllowedUser.user_id == user.id)
        .order_by(Folder.name)
        .all()
    )

    return templates.TemplateResponse(
        request, "documents/folder.html",
        {
            "folder": None,
            "breadcrumb": [],
            "subfolders": own_folders,
            "shared_folders": shared_folders,
            "folder_docs": [],
            "can_manage": False,
            "can_create_here": user.role == ROLE_ADMIN,
            "current_user": user,
            "csrf_token": generate_csrf_token(str(user.id)),
        },
    )


@router.post("/folders")
def create_folder(
    request: Request,
    name: str = Form(...),
    parent_id: str = Form(default=""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_ADMIN)),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre es obligatorio.")

    parent_id = parent_id.strip()
    parent: Folder | None = None
    depth = 1
    if parent_id:
        parent = db.query(Folder).filter(Folder.id == parent_id).first()
        if not parent or str(parent.owner_id) != str(user.id):
            raise HTTPException(404, "Carpeta padre no encontrada.")
        depth = parent.depth + 1
        if depth > FOLDER_MAX_DEPTH:
            raise HTTPException(400, f"Máximo {FOLDER_MAX_DEPTH} niveles de carpetas.")

    folder = Folder(name=name, owner_id=user.id, parent_id=parent.id if parent else None, depth=depth)
    db.add(folder)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(400, "Ya existe una carpeta con ese nombre en este nivel.")

    audit.log_action(
        "create_folder", user=user, request=request,
        resource_type="folder", resource_id=str(folder.id), resource_name=name,
    )
    return RedirectResponse(f"/documents/folders/{folder.id}", status_code=302)


@router.get("/folders/{folder_id}", response_class=HTMLResponse)
def folder_detail(
    folder_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(404)
    if not can_access_folder(user, folder):
        raise HTTPException(403)

    breadcrumb = [
        {"id": str(f.id), "name": f.name, "accessible": can_access_folder(user, f)}
        for f in reversed(folders_lib.ancestor_chain(db, folder.id))
    ]
    subfolders = sorted(
        db.query(Folder).filter(Folder.parent_id == folder.id).all(), key=lambda f: f.name
    )
    folder_docs = (
        db.query(Document).filter(Document.folder_id == folder.id).order_by(Document.title).all()
    )
    docs = []
    for d in folder_docs:
        docs.append({
            "id": str(d.id),
            "title": d.title,
            "status": d.status,
            "content_type": d.content_type,
            "is_drive": bool(d.drive_url),
            "can_manage": can_manage_document(user, d),
            **_preview_meta(str(d.id), d.content_type or "", d.drive_url),
        })

    return templates.TemplateResponse(
        request, "documents/folder.html",
        {
            "folder": folder,
            "breadcrumb": breadcrumb,
            "subfolders": subfolders,
            "shared_folders": [],
            "folder_docs": docs,
            "can_manage": can_manage_folder(user, folder),
            "can_create_here": can_manage_folder(user, folder) and folder.depth < FOLDER_MAX_DEPTH,
            "current_user": user,
            "csrf_token": generate_csrf_token(str(user.id)),
        },
    )


@router.post("/folders/{folder_id}/delete")
def delete_folder(
    folder_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(404)
    if not can_manage_folder(user, folder):
        raise HTTPException(403)

    parent_id = folder.parent_id
    name = folder.name
    # Documentos afectados (carpeta + subárbol): tras borrar quedan sin
    # carpeta (ON DELETE SET NULL) y hay que reindexarlos para que el índice
    # deje de reflejar el folder_shared_user_ids que ya no aplica.
    affected_folder_ids = folders_lib.descendant_folder_ids(db, folder.id)
    affected_doc_ids = [
        row[0] for row in db.query(Document.id).filter(Document.folder_id.in_(affected_folder_ids)).all()
    ]

    # Las subcarpetas se borran en cascada (ON DELETE CASCADE).
    db.delete(folder)
    db.commit()

    for doc_id_ in affected_doc_ids:
        doc = db.query(Document).filter(Document.id == doc_id_).first()
        if doc:
            _reindex_document_access(db, doc, background_tasks)

    audit.log_action(
        "delete_folder", user=user, request=request,
        resource_type="folder", resource_id=folder_id, resource_name=name,
    )
    dest = f"/documents/folders/{parent_id}" if parent_id else "/documents/folders"
    return RedirectResponse(dest, status_code=302)


@router.get("/folders/{folder_id}/share", response_class=HTMLResponse)
def folder_share_form(
    folder_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(404)
    if not can_manage_folder(user, folder):
        raise HTTPException(403)

    shared_users = (
        db.query(User)
        .join(FolderAllowedUser, FolderAllowedUser.user_id == User.id)
        .filter(FolderAllowedUser.folder_id == folder_id)
        .order_by(User.name, User.email)
        .all()
    )
    shared_ids = {str(u.id) for u in shared_users}
    candidates_query = db.query(User).filter(User.is_active == True)  # noqa: E712
    if shared_ids:
        candidates_query = candidates_query.filter(~User.id.in_(shared_ids))
    candidates = candidates_query.order_by(User.name, User.email).all()

    return templates.TemplateResponse(
        request, "documents/folder_share.html",
        {
            "folder": folder,
            "shared_users": shared_users,
            "candidates": candidates,
            "current_user": user,
            "csrf_token": generate_csrf_token(str(user.id)),
        },
    )


@router.post("/folders/{folder_id}/share")
def share_folder(
    folder_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    target_user_id: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(404)
    if not can_manage_folder(user, folder):
        raise HTTPException(403)

    target = db.query(User).filter(User.id == target_user_id, User.is_active == True).first()  # noqa: E712
    if not target:
        raise HTTPException(400, "Persona no encontrada o inactiva.")

    already_shared = db.query(FolderAllowedUser).filter(
        FolderAllowedUser.folder_id == folder_id,
        FolderAllowedUser.user_id == target_user_id,
    ).first()
    if not already_shared:
        db.add(FolderAllowedUser(folder_id=folder_id, user_id=target_user_id))
        db.commit()
        folders_lib.reindex_folder_subtree(db, folder.id, background_tasks)
        audit.log_action(
            "share_folder", user=user, request=request,
            resource_type="folder", resource_id=folder_id, resource_name=folder.name,
            details=f"shared_with={target.email}",
        )
    return RedirectResponse(f"/documents/folders/{folder_id}/share", status_code=302)


@router.post("/folders/{folder_id}/share/{target_user_id}/revoke")
def revoke_folder_share(
    folder_id: str,
    target_user_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    folder = db.query(Folder).filter(Folder.id == folder_id).first()
    if not folder:
        raise HTTPException(404)
    if not can_manage_folder(user, folder):
        raise HTTPException(403)

    grant = db.query(FolderAllowedUser).filter(
        FolderAllowedUser.folder_id == folder_id,
        FolderAllowedUser.user_id == target_user_id,
    ).first()
    if grant:
        target_email = grant.user.email if grant.user else target_user_id
        db.delete(grant)
        db.commit()
        folders_lib.reindex_folder_subtree(db, folder.id, background_tasks)
        audit.log_action(
            "revoke_folder_share", user=user, request=request,
            resource_type="folder", resource_id=folder_id, resource_name=folder.name,
            details=f"revoked_from={target_email}",
        )
    return RedirectResponse(f"/documents/folders/{folder_id}/share", status_code=302)
    return RedirectResponse("/documents/", status_code=302)
