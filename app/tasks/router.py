import re
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from jose import JWTError
from sqlalchemy import false, or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import decode_token, generate_csrf_token
from app.database import get_db, SessionLocal
from app import audit, rag, storage
from app.documents.router import _preview_meta
from app.messaging import realtime
from app.permissions import (
    build_access_filter, can_access_document, granted_department_ids, has_oversight, is_manager,
    branch_user_ids as _branch_user_ids, manager_scope as _manager_scope, scope_user_condition,
)
from app.models import (
    Branch, Department, Document, Project, RecurringTask, RecurringTaskShare, Task, TaskComment, TaskEvidence, TaskFollowUp,
    TaskShare, TaskStatusHistory, TaskTag, TaskTagAssignment, User,
    CALENDAR_REMINDER_OPTIONS,
    ROLE_SUPERADMIN, ROLE_AUDITOR,
    TASK_STATUSES, TASK_PRIORITIES, TASK_DONE, TASK_TAG_COLORS,
    RECURRENCE_FREQUENCIES,
)
from app.calendar.reminders import compute_remind_at, to_local, to_utc
from app.tasks.recurring import local_now, parse_recurrence
from app.templating import templates


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"

router = APIRouter(prefix="/tasks", tags=["tasks"])


# ── Visibility helpers ────────────────────────────────────────────────────────

def _tasks_query(user: User, db: Session, include_archived: bool = True, *, audit: bool = False):
    """Tareas visibles para el usuario. `audit=True` solo en vistas de lectura:
    ahí el auditor ve las de todos los departamentos; para participar
    (comentar, subir evidencias) conserva el alcance normal de empleado."""
    q = db.query(Task).options(
        joinedload(Task.assignee),
        joinedload(Task.created_by_user),
        joinedload(Task.department),
        joinedload(Task.project),
        joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
    )
    if user.role != ROLE_SUPERADMIN and not (audit and user.role == ROLE_AUDITOR):
        conditions = [
            Task.created_by == user.id,
            Task.assigned_to == user.id,
        ]
        if user.department_id:
            conditions.append(Task.department_id == user.department_id)
        granted = granted_department_ids(user)
        if granted:
            conditions.append(Task.department_id.in_(granted))
        q = q.filter(or_(*conditions))
    if not include_archived:
        q = q.filter(Task.archived_at.is_(None))
    return q


def _effective_department_id(department_id, assigned_to, db: Session):
    """Departamento con el que queda una tarea. Si no se eligió uno pero hay
    asignado (p.ej. autoasignación), hereda el del asignado: sin esto la tarea
    queda con department_id NULL y el admin del área no la ve ni la gestiona."""
    if department_id:
        return department_id
    if assigned_to:
        return db.query(User.department_id).filter(User.id == assigned_to).scalar()
    return None


def _can_edit_task(user: User, task: Task, db: Session) -> bool:
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(task.created_by) == str(user.id):
        return True
    scope = _manager_scope(user, db)
    if task.department_id and task.department_id in scope["dept_ids"]:
        return True
    if task.assigned_to and task.assigned_to in _branch_user_ids(scope, db):
        return True
    return False


def _can_update_status(user: User, task: Task, db: Session) -> bool:
    if _can_edit_task(user, task, db):
        return True
    return task.assigned_to and str(task.assigned_to) == str(user.id)


def _can_approve_task(user: User, task: Task) -> bool:
    """Sólo quien asignó la tarea (su creador) o un superadmin puede aprobarla
    (marcarla Lista). El asignado puede llevarla hasta Revisión, no más allá."""
    if user.role == ROLE_SUPERADMIN:
        return True
    return str(task.created_by) == str(user.id)


def _is_shared_with(user: User, task_id, db: Session) -> bool:
    return db.query(TaskShare).filter(TaskShare.task_id == task_id, TaskShare.user_id == user.id).first() is not None


def _can_view_task(user: User, task_id, db: Session) -> bool:
    """La ve por su alcance (incluye la lectura de auditor) o porque se la compartieron."""
    return (
        _tasks_query(user, db, audit=True).filter(Task.id == task_id).first() is not None
        or _is_shared_with(user, task_id, db)
    )


def _can_comment(user: User, task_id, db: Session) -> bool:
    """Comenta quien participa por su alcance o a quien se la compartieron (el
    auditor fuera de su alcance solo lee)."""
    return (
        _tasks_query(user, db).filter(Task.id == task_id).first() is not None
        or _is_shared_with(user, task_id, db)
    )


def _can_share(user: User, task: Task, db: Session) -> bool:
    """Comparte quien puede editarla y el asignado; a quien se la compartieron, no."""
    return _can_edit_task(user, task, db) or (task.assigned_to and str(task.assigned_to) == str(user.id))


def _can_manage_follow_ups(user: User, task: Task, db: Session) -> bool:
    """Programa y cierra seguimientos quien edita la tarea y su asignado."""
    return _can_edit_task(user, task, db) or (task.assigned_to and str(task.assigned_to) == str(user.id))


def _followers(task: Task) -> set:
    """Quienes reciben los avisos de la tarea: asignado, creador y con quienes se compartió."""
    return {task.assigned_to, task.created_by} | {s.user_id for s in task.shares}


def _share_candidates(user: User, db: Session) -> list[User]:
    """Cualquier persona activa de cualquier departamento (el selector filtra por
    departamento y nombre en el cliente)."""
    return (
        db.query(User).options(joinedload(User.department))
        .filter(User.is_active == True, User.id != user.id)  # noqa: E712
        .order_by(User.name, User.email).all()
    )


def _add_shares(task: Task, user_ids: list[str], actor: User, db: Session) -> list:
    """Agrega a quienes falten (activos; ni el creador ni el asignado, que ya la
    siguen). Devuelve los ids agregados para avisarles tras el commit."""
    wanted = {uid for uid in user_ids if uid}
    if not wanted:
        return []
    existing = {str(s.user_id) for s in db.query(TaskShare.user_id).filter(TaskShare.task_id == task.id).all()}
    skip = existing | {str(task.created_by), str(task.assigned_to), str(actor.id)}
    try:
        valid = {
            row[0] for row in db.query(User.id).filter(
                User.id.in_([uuid.UUID(uid) for uid in wanted - skip]), User.is_active == True,  # noqa: E712
            ).all()
        } if wanted - skip else set()
    except ValueError:
        raise HTTPException(400, "Persona inválida")
    for uid in valid:
        db.add(TaskShare(task_id=task.id, user_id=uid, shared_by=actor.id))
    return list(valid)


def _notify_shared(task: Task, user_ids: list, actor: User) -> None:
    name = actor.name or actor.email
    for uid in user_ids:
        realtime.notify_user(
            uid, title=f"{name} compartió una tarea contigo", body=task.title,
            url=f"/tasks/{task.id}", kind="task_shared",
        )


def _status_durations(task: Task) -> dict:
    """Tiempo acumulado en cada estado (sin contar "Listo", que es terminal y no
    aporta al desglose) más el tiempo total que tardó la tarea, de punta a punta.

    El tramo del estado actual se cuenta hasta ahora, así que el desglose siempre
    refleja algo aunque la tarea siga abierta. El total, en cambio, sólo tiene
    sentido una vez la tarea se aprobó (pasó a "Listo"); mientras no llegue a ese
    estado, `total_seconds` es None. `archived_at` no participa: solo indica que
    se sacó del Kanban (siempre después de "Listo"), no cuándo se completó.
    """
    history = task.status_history
    if not history:
        return {"breakdown": [], "total_seconds": None}
    totals = {s: 0.0 for s in TASK_STATUSES}
    now_end = datetime.utcnow()
    for i, entry in enumerate(history):
        segment_end = history[i + 1].changed_at if i + 1 < len(history) else now_end
        seconds = (segment_end - entry.changed_at).total_seconds()
        if entry.to_status in totals and seconds > 0:
            totals[entry.to_status] += seconds

    finished_at = history[-1].changed_at if task.status == TASK_DONE else None

    return {
        "breakdown": [
            {"status": s, "seconds": totals[s]}
            for s in TASK_STATUSES if s != TASK_DONE and totals[s] > 0
        ],
        "total_seconds": (finished_at - history[0].changed_at).total_seconds() if finished_at else None,
    }


# ── Document reference helpers ─────────────────────────────────────────────────

def _accessible_documents(user: User) -> list[dict]:
    """Documentos que el usuario puede referenciar en una tarea (access-filtered).

    Reusa la búsqueda con el filtro de visibilidad del módulo Documentos, así que
    respeta rol/departamento. Devuelve dicts con id/title/department_name/content_type.
    """
    try:
        return rag.search_documents("", build_access_filter(user), size=500)
    except Exception:
        return []


def _validated_document_id(document_id: str, user: User, db: Session) -> str | None:
    """Devuelve el id si el documento existe y el usuario puede accederlo; si no, None."""
    if not document_id:
        return None
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc or not can_access_document(user, doc):
        return None
    return document_id


# ── Tag helpers ────────────────────────────────────────────────────────────────

def _validated_tag_ids(tag_ids: list[str], department_id: str | None, db: Session) -> list[str]:
    """Filtra a los ids de etiquetas que existen y pertenecen al departamento dado.
    Una tarea sin departamento no puede llevar etiquetas (son siempre por depto)."""
    tag_ids = [t for t in dict.fromkeys(tag_ids) if t]
    if not tag_ids or not department_id:
        return []
    valid = {
        str(row[0]) for row in db.query(TaskTag.id).filter(
            TaskTag.department_id == department_id, TaskTag.id.in_(tag_ids)
        ).all()
    }
    return [t for t in tag_ids if t in valid]


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def list_tasks(
    request: Request,
    tab: str = "",
    dept_id: str = "",
    user_id: str = "",
    search: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    is_admin = is_manager(current_user)
    show_dept_tab = has_oversight(current_user)
    is_auditor = current_user.role == ROLE_AUDITOR

    # Available tabs — "dept" para admins/superadmins y quien tenga acceso extra
    # a otros departamentos; "all" (solo lectura, todos los departamentos) para auditores.
    valid_tabs = ["assigned", "created", "shared"] + (["dept"] if show_dept_tab else []) + (["all"] if is_auditor else [])
    if tab not in valid_tabs:
        tab = "assigned"

    base = db.query(Task).options(
        joinedload(Task.assignee),
        joinedload(Task.created_by_user),
        joinedload(Task.department),
        joinedload(Task.project),
        joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
    ).filter(Task.archived_at.is_(None))

    # Data for create form. "departments" es difusión abierta (cualquier admin
    # puede difundir a cualquier departamento); "users" es asignación puntual,
    # acotada al alcance de cada rol (ver _assignable_users_query).
    departments = db.query(Department).order_by(Department.name).all()
    users = _assignable_users_query(current_user, db).all()
    projects = db.query(Project).order_by(Project.name).all()
    tags = db.query(TaskTag).order_by(TaskTag.name).all()

    # Filtro por departamento en las vistas personales y para superadmin.
    # En la vista Departamento, los administradores filtran por persona
    # dentro de su alcance de gestión.
    filter_departments = []
    filter_users = []
    filter_dept_id = ""
    filter_user_id = ""

    if tab == "assigned":
        q = base.filter(Task.assigned_to == current_user.id)
        can_drag = True  # the assignee may move their own tasks between statuses
    elif tab == "created":
        q = base.filter(Task.created_by == current_user.id)
        can_drag = False
    elif tab == "shared":
        q = base.filter(Task.shares.any(TaskShare.user_id == current_user.id))
        can_drag = False
    elif tab == "all":  # auditor: todas las tareas, solo lectura
        q = base
        can_drag = False
    else:  # dept
        # Mismo organigrama que las plantillas recurrentes: superadmin → todo;
        # admin → su departamento (en Comercial, las tareas de la gente de sus
        # sucursales); más los departamentos con acceso extra (view o manage).
        scope = _manager_scope(current_user, db)
        if scope is None:
            q = base  # superadmin ve todos los departamentos
            # Filtro por departamento (solo superadmin).
            filter_departments = departments
        else:
            dept_ids = scope["dept_ids"] | granted_department_ids(current_user)
            conds = []
            if dept_ids:
                conds.append(Task.department_id.in_(dept_ids))
            branch_user_ids = _branch_user_ids(scope, db)
            if branch_user_ids:
                conds.append(Task.assigned_to.in_(branch_user_ids))
            q = base.filter(or_(*conds)) if conds else base.filter(false())

            # Filtro por persona: las de su alcance y las de sus departamentos
            # con acceso extra.
            user_q = db.query(User).filter(User.is_active == True)
            in_scope = scope_user_condition({**scope, "dept_ids": dept_ids})
            user_q = user_q.filter(in_scope if in_scope is not None else false())
            filter_users = user_q.order_by(User.email).all()
            if user_id and any(str(u.id) == user_id for u in filter_users):
                q = q.filter(Task.assigned_to == user_id)
                filter_user_id = user_id
        can_drag = False

    if tab in ("assigned", "created", "shared", "all"):
        filter_departments = departments
    selected_department = next((d for d in filter_departments if str(d.id) == dept_id), None)
    if selected_department is not None:
        q = q.filter(Task.department_id == selected_department.id)
        filter_dept_id = dept_id

    search = search.strip()
    if search:
        q = q.filter(or_(
            Task.title.icontains(search, autoescape=True),
            Task.description.icontains(search, autoescape=True),
        ))
    tasks = q.order_by(Task.created_at.desc()).all()

    # Group into Kanban columns keyed by status.
    columns = {s: [] for s in TASK_STATUSES}
    for t in tasks:
        columns.setdefault(t.status, []).append(t)

    csrf = generate_csrf_token(str(current_user.id))
    return templates.TemplateResponse(
        request,
        "tasks/list.html",
        {
            "current_user": current_user,
            "columns": columns,
            "tasks": tasks,
            "total": len(tasks),
            "search": search,
            "tab": tab,
            "is_admin": is_admin,
            "show_dept_tab": show_dept_tab,
            "is_auditor": is_auditor,
            "can_drag": can_drag,
            "departments": departments,
            "users": users,
            "projects": projects,
            "tags": tags,
            "documents": _accessible_documents(current_user),
            "share_candidates": _share_candidates(current_user, db),
            "filter_departments": filter_departments,
            "filter_users": filter_users,
            "filter_dept_id": filter_dept_id,
            "filter_user_id": filter_user_id,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "today": date.today().isoformat(),
            "csrf_token": csrf,
        },
    )


# ── Archived (independent, table-only view) ────────────────────────────────────

@router.get("/archived", response_class=HTMLResponse)
def list_archived_tasks(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    tasks = (
        _tasks_query(current_user, db, audit=True)
        .filter(Task.archived_at.isnot(None))
        .order_by(Task.archived_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        request,
        "tasks/archived.html",
        {
            "current_user": current_user,
            "tasks": tasks,
            "today": date.today().isoformat(),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


# ── Live updates (SSE) ──────────────────────────────────────────────────────

@router.get("/stream")
async def tasks_stream(request: Request):
    """SSE: broadcasts when any task changes, so every open board refreshes live.

    Registered before GET /{task_id} so it isn't captured by that catch-all.
    Authenticated from the cookie directly (like messaging's /signals) so no DB
    connection is held for the lifetime of the stream.
    """
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(401)
    try:
        decode_token(token)
    except JWTError:
        raise HTTPException(401)

    return StreamingResponse(
        realtime.tasks_event_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("/")
def create_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    status: str = Form("pending"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_id: str = Form(""),
    document_id: str = Form(""),
    due_date: str = Form(""),
    next_url: str = Form(""),
    tag_ids: list[str] = Form(default=[]),
    shared_with: list[str] = Form(default=[]),
    evidences: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    storage.check_upload_sizes(*evidences)
    _validate_target_scope(current_user, db, department_id, assigned_to, required=False)

    task = Task(
        id=uuid.uuid4(),
        title=title.strip(),
        description=description.strip() or None,
        priority=priority if priority in TASK_PRIORITIES else "medium",
        status=status if status in TASK_STATUSES else "pending",
        department_id=_effective_department_id(department_id, assigned_to, db),
        assigned_to=assigned_to if assigned_to else None,
        project_id=project_id if project_id else None,
        document_id=_validated_document_id(document_id, current_user, db),
        created_by=current_user.id,
        due_date=date.fromisoformat(due_date) if due_date else None,
    )
    db.add(task)
    db.flush()
    if task.status == TASK_DONE:
        task.completed_at = task.created_at
    db.add(TaskStatusHistory(
        id=uuid.uuid4(),
        task_id=task.id,
        from_status=None,
        to_status=task.status,
        changed_by=current_user.id,
        changed_at=task.created_at,
    ))
    for tid in _validated_tag_ids(tag_ids, task.department_id, db):
        db.add(TaskTagAssignment(task_id=task.id, tag_id=tid))
    shared_ids = _add_shares(task, shared_with, current_user, db)

    for f in evidences:
        if not f.filename:
            continue
        content = f.file.read()
        safe = _safe_filename(f.filename)
        key = f"tasks/{task.id}/{uuid.uuid4()}_{safe}"
        storage.upload_evidence(key, content, f.content_type or "application/octet-stream")
        db.add(TaskEvidence(
            id=uuid.uuid4(),
            task_id=task.id,
            uploaded_by=current_user.id,
            filename=f.filename,
            file_key=key,
            content_type=f.content_type or "application/octet-stream",
            file_size=len(content),
        ))

    db.commit()
    if task.assigned_to and str(task.assigned_to) != str(current_user.id):
        actor = current_user.name or current_user.email
        realtime.notify_user(
            task.assigned_to,
            title=f"{actor} te asignó una tarea",
            body=task.title,
            url=f"/tasks/{task.id}",
            kind="task_assigned",
        )
    _notify_shared(task, shared_ids, current_user)
    realtime.notify_tasks()
    audit.log_action(
        "task_create", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"priority={task.priority} evidences={len([f for f in evidences if f.filename])} shared={len(shared_ids)}",
    )
    redirect = next_url if next_url and next_url.startswith("/") else "/tasks/"
    return RedirectResponse(redirect, status_code=302)


# ── Detail ────────────────────────────────────────────────────────────────────

@router.get("/{task_id}", response_class=HTMLResponse)
def task_detail(
    task_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)

    task = (
        db.query(Task)
        .options(
            joinedload(Task.assignee),
            joinedload(Task.created_by_user),
            joinedload(Task.department),
            joinedload(Task.project),
            joinedload(Task.comments).joinedload(TaskComment.user),
            joinedload(Task.evidences).joinedload(TaskEvidence.uploader),
            joinedload(Task.document),
            joinedload(Task.status_history),
            joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
            joinedload(Task.shares).joinedload(TaskShare.user).joinedload(User.department),
            joinedload(Task.follow_ups).joinedload(TaskFollowUp.done_by_user),
        )
        .filter(Task.id == task_id)
        .first()
    )
    if not task:
        # La tarea ya no existe (borrada por otra pestaña/persona, o link viejo).
        # Redirigir en vez de 404 crudo: el SSE de esta misma página hace
        # location.reload() al recibir el aviso de "tarea cambiada" que dispara
        # el propio borrado, y esa recarga puede ganarle a la del HX-Redirect
        # del botón Eliminar — sin esto, aterriza en un JSON de error.
        return RedirectResponse("/tasks/", status_code=302)

    # Verify visibility (por su alcance o porque se la compartieron)
    in_scope = _tasks_query(current_user, db, audit=True).filter(Task.id == task_id).first() is not None
    if not in_scope and not _is_shared_with(current_user, task_id, db):
        raise HTTPException(403)
    # El auditor puede ver tareas ajenas a su alcance, pero no participar en ellas;
    # a quien se la compartieron solo comenta (no sube evidencias).
    can_participate = in_scope and (
        current_user.role != ROLE_AUDITOR
        or _tasks_query(current_user, db).filter(Task.id == task_id).first() is not None
    )
    can_share = _can_share(current_user, task, db)

    users = _assignable_users_query(current_user, db).all()
    departments = db.query(Department).order_by(Department.name).all()
    dept_tags = (
        db.query(TaskTag).filter(TaskTag.department_id == task.department_id).order_by(TaskTag.name).all()
        if task.department_id else []
    )
    selected_tag_ids = {str(tt.tag_id) for tt in task.tags}

    # Metadatos de previsualización del documento vinculado (mismo criterio que las
    # cards de Documentos): Drive/PDF/imagen/HTML → iframe; el resto solo enlace.
    doc_preview = (
        _preview_meta(str(task.document.id), task.document.content_type, task.document.drive_url)
        if task.document else None
    )

    return templates.TemplateResponse(
        request,
        "tasks/detail.html",
        {
            "current_user": current_user,
            "task": task,
            "status_durations": _status_durations(task),
            "doc_preview": doc_preview,
            "users": users,
            "departments": departments,
            "dept_tags": dept_tags,
            "selected_tag_ids": selected_tag_ids,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "can_edit": _can_edit_task(current_user, task, db),
            "can_update_status": _can_update_status(current_user, task, db),
            "is_admin": is_manager(current_user),
            "can_approve": _can_approve_task(current_user, task),
            "can_participate": can_participate,
            "can_comment": _can_comment(current_user, task_id, db),
            "can_share": can_share,
            "share_candidates": _share_candidates(current_user, db) if can_share else [],
            "can_follow_up": _can_manage_follow_ups(current_user, task, db),
            "follow_ups": [_follow_up_view(f) for f in task.follow_ups],
            "reminder_options": CALENDAR_REMINDER_OPTIONS,
            "local_today": local_now().date().isoformat(),
            "today": date.today().isoformat(),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


# ── Live updates (SSE) ──────────────────────────────────────────────────────

@router.get("/{task_id}/stream")
async def task_stream(task_id: str, request: Request):
    """SSE: broadcasts when this task changes, so its detail page refreshes live.

    Authenticated from the cookie directly (like /tasks/stream) so no DB
    connection is held for the lifetime of the stream — visibility is checked
    once, up front, with a short-lived session.
    """
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(401)
    try:
        user_id = decode_token(token).get("sub")
    except JWTError:
        raise HTTPException(401)
    if not user_id:
        raise HTTPException(401)

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        visible = user and _can_view_task(user, task_id, db)
    finally:
        db.close()
    if not visible:
        raise HTTPException(403)

    return StreamingResponse(
        realtime.task_event_stream(task_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Evidences ─────────────────────────────────────────────────────────────────

@router.post("/{task_id}/evidences")
def upload_evidences(
    task_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    if not _tasks_query(current_user, db).filter(Task.id == task_id).first():
        raise HTTPException(403)

    storage.check_upload_sizes(*files)

    for f in files:
        if not f.filename:
            continue
        content = f.file.read()
        safe = _safe_filename(f.filename)
        key = f"tasks/{task_id}/{uuid.uuid4()}_{safe}"
        storage.upload_evidence(key, content, f.content_type or "application/octet-stream")
        db.add(TaskEvidence(
            id=uuid.uuid4(),
            task_id=task.id,
            uploaded_by=current_user.id,
            filename=f.filename,
            file_key=key,
            content_type=f.content_type or "application/octet-stream",
            file_size=len(content),
        ))
    db.commit()
    uploaded = [f.filename for f in files if f.filename]
    if uploaded:
        realtime.notify_task(task_id)
    task_obj = db.query(Task).filter(Task.id == task_id).first()
    audit.log_action(
        "evidence_upload", user=current_user, request=request,
        resource_type="task", resource_id=task_id,
        resource_name=task_obj.title if task_obj else task_id,
        details=f"files={len(uploaded)} names={','.join(uploaded[:5])}",
    )
    # Notifica al asignado y al creador (menos a quien sube), solo si hubo archivos.
    if uploaded:
        actor = current_user.name or current_user.email
        for uid in _followers(task):
            if uid and str(uid) != str(current_user.id):
                realtime.notify_user(
                    uid,
                    title=f"{actor} subió evidencia",
                    body=task.title,
                    url=f"/tasks/{task_id}",
                    kind="task_evidence",
                )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.post("/{task_id}/evidences/{evidence_id}/delete")
def delete_evidence(
    task_id: str,
    evidence_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    ev = db.query(TaskEvidence).filter(
        TaskEvidence.id == evidence_id,
        TaskEvidence.task_id == task_id,
    ).first()
    if not ev:
        raise HTTPException(404)

    task = db.query(Task).filter(Task.id == task_id).first()
    is_uploader = str(ev.uploaded_by) == str(current_user.id)
    if not is_uploader and not _can_edit_task(current_user, task, db):
        raise HTTPException(403)

    filename = ev.filename
    try:
        storage.delete_evidence(ev.file_key)
    except Exception:
        pass
    db.delete(ev)
    db.commit()
    realtime.notify_task(task_id)
    audit.log_action(
        "evidence_delete", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=filename,
    )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.get("/{task_id}/evidences/{evidence_id}/download")
def download_evidence(
    task_id: str,
    evidence_id: str,
    inline: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not _can_view_task(current_user, task_id, db):
        raise HTTPException(403)

    ev = db.query(TaskEvidence).filter(
        TaskEvidence.id == evidence_id,
        TaskEvidence.task_id == task_id,
    ).first()
    if not ev:
        raise HTTPException(404)

    url = storage.get_evidence_url(ev.file_key, ev.filename, inline=inline)
    from fastapi.responses import RedirectResponse as RR
    return RR(url, status_code=302)


# ── Update status (HTMX) ──────────────────────────────────────────────────────

@router.post("/{task_id}/status", response_class=HTMLResponse)
def update_status(
    task_id: str,
    request: Request,
    status: str = Form(...),
    mode: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_update_status(current_user, task, db):
        raise HTTPException(403)
    if status not in TASK_STATUSES:
        raise HTTPException(400)
    prev = task.status
    is_approval = status == TASK_DONE and prev != TASK_DONE
    if is_approval and not _can_approve_task(current_user, task):
        raise HTTPException(403, "Solo quien asignó la tarea o un superadmin puede aprobarla")
    task.status = status
    if status != prev:
        task.completed_at = datetime.utcnow() if status == TASK_DONE else None
        db.add(TaskStatusHistory(
            id=uuid.uuid4(),
            task_id=task.id,
            from_status=prev,
            to_status=status,
            changed_by=current_user.id,
        ))
    db.commit()
    # Notify the other party (assignee/creator) that the status changed.
    actor = current_user.name or current_user.email
    push_title = f"{actor} aprobó tu tarea" if is_approval else f"{actor} cambió el estado"
    for uid in _followers(task):
        if uid and str(uid) != str(current_user.id):
            realtime.notify_user(
                uid, title=push_title, body=task.title, url=f"/tasks/{task_id}",
                kind="task_approved" if is_approval else "task_status",
            )
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_status_change", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"{prev} → {status}",
    )
    if is_approval:
        audit.log_action(
            "task_approve", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
        )
    # Kanban drag-and-drop: no redirect, the card already moved client-side.
    if mode == "kanban":
        return HTMLResponse(status_code=204)
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


# ── Archive / unarchive ────────────────────────────────────────────────────────

@router.post("/{task_id}/archive")
def archive_task(
    task_id: str,
    request: Request,
    next_url: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_update_status(current_user, task, db):
        raise HTTPException(403)
    if task.status != TASK_DONE:
        raise HTTPException(400, "Solo se pueden archivar tareas Listo")
    task.archived_at = datetime.utcnow()
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_archive", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
    )
    redirect = next_url if next_url and next_url.startswith("/") else "/tasks/"
    return RedirectResponse(redirect, status_code=302)


@router.post("/{task_id}/unarchive")
def unarchive_task(
    task_id: str,
    request: Request,
    next_url: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_update_status(current_user, task, db):
        raise HTTPException(403)
    task.archived_at = None
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_unarchive", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
    )
    redirect = next_url if next_url and next_url.startswith("/") else "/tasks/archived"
    return RedirectResponse(redirect, status_code=302)


# ── Assign (HTMX) ─────────────────────────────────────────────────────────────

@router.post("/{task_id}/assign", response_class=HTMLResponse)
def assign_task(
    task_id: str,
    request: Request,
    assigned_to: str = Form(""),
    department_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    _validate_target_scope(current_user, db, department_id, assigned_to, required=False)
    old_dept_id = task.department_id
    task.assigned_to = assigned_to if assigned_to else None
    task.department_id = department_id if department_id else (
        task.department_id or _effective_department_id(None, assigned_to, db)
    )
    if department_id and str(task.department_id) != str(old_dept_id):
        # Las etiquetas son por departamento: si cambia, las del depto anterior dejan de aplicar.
        db.query(TaskTagAssignment).filter(TaskTagAssignment.task_id == task_id).delete()
    db.commit()
    if task.assigned_to and str(task.assigned_to) != str(current_user.id):
        actor = current_user.name or current_user.email
        realtime.notify_user(
            task.assigned_to,
            title=f"{actor} te asignó una tarea",
            body=task.title,
            url=f"/tasks/{task_id}",
            kind="task_assigned",
        )
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    assignee = db.query(User).filter(User.id == assigned_to).first() if assigned_to else None
    audit.log_action(
        "task_assign", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"assigned_to={assignee.email if assignee else 'none'}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


# ── Tags (HTMX) ───────────────────────────────────────────────────────────────

@router.post("/{task_id}/tags", response_class=HTMLResponse)
def set_task_tags(
    task_id: str,
    request: Request,
    tag_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)

    valid_ids = _validated_tag_ids(tag_ids, str(task.department_id) if task.department_id else None, db)
    db.query(TaskTagAssignment).filter(TaskTagAssignment.task_id == task_id).delete()
    for tid in valid_ids:
        db.add(TaskTagAssignment(task_id=task_id, tag_id=tid))
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_tags_update", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=task.title,
        details=f"tags={len(valid_ids)}",
    )
    return HTMLResponse(headers={"HX-Redirect": f"/tasks/{task_id}"})


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{task_id}", response_class=HTMLResponse)
def delete_task(
    task_id: str,
    request: Request,
    back: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_edit_task(current_user, task, db):
        raise HTTPException(403)
    title = task.title
    db.delete(task)
    db.commit()
    realtime.notify_tasks()
    realtime.notify_task(task_id)
    audit.log_action(
        "task_delete", user=current_user, request=request,
        resource_type="task", resource_id=task_id, resource_name=title,
    )
    # Vuelve a la lista de origen (tab/filtros); solo rutas propias de /tasks/.
    redirect = back if back.startswith("/tasks/") and "//" not in back else "/tasks/"
    return HTMLResponse(headers={"HX-Redirect": redirect})


# ── Shares (seguimiento) ─────────────────────────────────────────────────────

@router.post("/{task_id}/shares")
def add_task_shares(
    task_id: str,
    request: Request,
    shared_with: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task or not _can_share(current_user, task, db):
        raise HTTPException(403)
    added = _add_shares(task, shared_with, current_user, db)
    db.commit()
    if added:
        _notify_shared(task, added, current_user)
        realtime.notify_task(task_id)
        audit.log_action(
            "task_share", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
            details=f"users={len(added)}",
        )
    return RedirectResponse(f"/tasks/{task_id}", status_code=302)


@router.post("/{task_id}/shares/{user_id}/remove")
def remove_task_share(
    task_id: str,
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    try:
        user_id = str(uuid.UUID(user_id))
    except ValueError:
        raise HTTPException(404)
    # Quita quien puede compartir, o la propia persona deja de seguirla.
    is_self = user_id == str(current_user.id)
    if not (is_self or _can_share(current_user, task, db)):
        raise HTTPException(403)
    share = db.query(TaskShare).filter(TaskShare.task_id == task.id, TaskShare.user_id == user_id).first()
    if share:
        db.delete(share)
        db.commit()
        realtime.notify_task(task_id)
        audit.log_action(
            "task_unshare", user=current_user, request=request,
            resource_type="task", resource_id=task_id, resource_name=task.title,
            details=f"user={user_id}",
        )
    return RedirectResponse("/tasks/?tab=shared" if is_self else f"/tasks/{task_id}", status_code=302)


# ── Comments (HTMX append) ───────────────────────────────────────────────────

@router.post("/{task_id}/comments", response_class=HTMLResponse)
def add_comment(
    task_id: str,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    # Comenta quien participa por su alcance o a quien se la compartieron
    if not _can_comment(current_user, task_id, db):
        raise HTTPException(403)

    comment = TaskComment(
        id=uuid.uuid4(),
        task_id=task.id,
        user_id=current_user.id,
        content=content.strip(),
    )
    db.add(comment)
    db.commit()
    db.refresh(comment)
    comment.user = current_user  # for template rendering

    # Notifica al asignado, al creador y a con quienes se compartió (menos a quien comenta).
    actor = current_user.name or current_user.email
    for uid in _followers(task):
        if uid and str(uid) != str(current_user.id):
            realtime.notify_user(
                uid,
                title=f"{actor} comentó",
                body=comment.content[:120],
                url=f"/tasks/{task.id}",
                kind="task_comment",
            )
    realtime.notify_task(task.id)
    audit.log_action(
        "task_comment", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=(comment.content or "")[:80],
    )

    return templates.TemplateResponse(
        request,
        "tasks/_comment.html",
        {
            "comment": comment,
            "current_user": current_user,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.post("/{task_id}/comments/{comment_id}/edit", response_class=HTMLResponse)
def edit_comment(
    task_id: str,
    comment_id: str,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    comment = db.query(TaskComment).filter(
        TaskComment.id == comment_id, TaskComment.task_id == task_id
    ).first()
    if not comment:
        raise HTTPException(404)
    # Solo el autor puede editar su comentario.
    if str(comment.user_id) != str(current_user.id):
        raise HTTPException(403)

    new_content = content.strip()
    if not new_content:
        raise HTTPException(400, "El comentario no puede quedar vacío")
    comment.content = new_content
    comment.edited = True
    db.commit()
    db.refresh(comment)
    comment.user = current_user  # for template rendering
    realtime.notify_task(task_id)

    return templates.TemplateResponse(
        request,
        "tasks/_comment.html",
        {
            "comment": comment,
            "current_user": current_user,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@router.post("/{task_id}/comments/{comment_id}/delete", response_class=HTMLResponse)
def delete_comment(
    task_id: str,
    comment_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    comment = db.query(TaskComment).filter(
        TaskComment.id == comment_id, TaskComment.task_id == task_id
    ).first()
    if not comment:
        raise HTTPException(404)
    task = db.query(Task).filter(Task.id == task_id).first()
    # Borra el autor, o quien pueda editar la tarea (creador/admin del depto/superadmin).
    is_author = str(comment.user_id) == str(current_user.id)
    if not (is_author or (task and _can_edit_task(current_user, task, db))):
        raise HTTPException(403)

    db.delete(comment)
    db.commit()
    realtime.notify_task(task_id)
    # Cuerpo vacío + hx-swap="outerHTML" → HTMX elimina el comentario del DOM.
    return HTMLResponse("")


# ── Seguimientos (bloques con hora en el calendario del responsable) ─────────

def _follow_up_view(f: TaskFollowUp) -> dict:
    """Datos para pintar un seguimiento en el detalle (horas en hora local)."""
    start, end = to_local(f.start_at), to_local(f.end_at)
    return {
        "f": f,
        "date": start.date().isoformat(),
        "start": f"{start:%H:%M}",
        "end": f"{end:%H:%M}",
        "label": f"{start:%d/%m/%Y} · {start:%H:%M}–{end:%H:%M}",
        "past": f.end_at < datetime.utcnow(),
    }


def _parse_follow_up(task: Task, day: str, start: str, end: str, reminder_minutes: str, existing=None):
    """Valida un seguimiento (hora local) y devuelve (start_utc, end_utc, reminder).

    El día debe caer entre hoy y la fecha límite de la tarea; al editar se
    permite conservar un día que ya pasó."""
    try:
        d = date.fromisoformat(day)
        start_local = datetime.combine(d, datetime.strptime(start, "%H:%M").time())
        end_local = datetime.combine(d, datetime.strptime(end, "%H:%M").time())
    except ValueError:
        raise HTTPException(422, "Fecha u hora inválida")
    if end_local <= start_local:
        raise HTTPException(422, "La hora de fin debe ser posterior a la de inicio")
    if task.due_date and d > task.due_date:
        raise HTTPException(422, "El seguimiento debe ser antes o el día de la fecha límite")
    keeps_day = existing is not None and to_local(existing.start_at).date() == d
    if d < local_now().date() and not keeps_day:
        raise HTTPException(422, "El seguimiento no puede ser en un día que ya pasó")
    if reminder_minutes in ("", None):
        reminder = None
    else:
        try:
            reminder = int(reminder_minutes)
        except ValueError:
            raise HTTPException(422, "Recordatorio inválido")
        if reminder not in CALENDAR_REMINDER_OPTIONS:
            raise HTTPException(422, "Recordatorio inválido")
    return to_utc(start_local), to_utc(end_local), reminder


def _follow_up_task(task_id: str, user: User, db: Session) -> Task:
    try:
        uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(404)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(404)
    if not _can_manage_follow_ups(user, task, db):
        raise HTTPException(403)
    return task


def _task_follow_up(task: Task, follow_up_id: str, db: Session) -> TaskFollowUp:
    try:
        uuid.UUID(follow_up_id)
    except ValueError:
        raise HTTPException(404)
    f = db.query(TaskFollowUp).filter(TaskFollowUp.id == follow_up_id, TaskFollowUp.task_id == task.id).first()
    if not f:
        raise HTTPException(404)
    return f


def _schedule_follow_up(f: TaskFollowUp, start_at: datetime, end_at: datetime, reminder: int | None) -> None:
    if (f.start_at, f.reminder_minutes) != (start_at, reminder):
        f.remind_at = compute_remind_at(start_at, False, reminder)
        f.reminded_at = None
    f.start_at, f.end_at, f.reminder_minutes = start_at, end_at, reminder


@router.post("/{task_id}/follow-ups")
def add_follow_up(
    task_id: str,
    request: Request,
    day: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    reminder_minutes: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    if task.status == TASK_DONE or task.archived_at:
        raise HTTPException(422, "La tarea ya está terminada")
    start_at, end_at, reminder = _parse_follow_up(task, day, start, end, reminder_minutes)

    f = TaskFollowUp(task_id=task.id, note=note.strip()[:500] or None, created_by=current_user.id)
    _schedule_follow_up(f, start_at, end_at, reminder)
    db.add(f)
    db.commit()

    # Avisa al asignado si el seguimiento lo programó alguien más.
    if task.assigned_to and task.assigned_to != current_user.id:
        realtime.notify_user(
            task.assigned_to,
            title=f"{current_user.name or current_user.email} programó un seguimiento",
            body=f"{task.title} · {to_local(start_at):%d/%m %H:%M}",
            url=f"/tasks/{task.id}",
            kind="task_follow_up",
        )
    realtime.notify_task(task.id)
    audit.log_action(
        "task_follow_up_add", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"{to_local(start_at):%Y-%m-%d %H:%M}-{to_local(end_at):%H:%M}",
    )
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/edit")
def edit_follow_up(
    task_id: str,
    follow_up_id: str,
    day: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    reminder_minutes: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    start_at, end_at, reminder = _parse_follow_up(task, day, start, end, reminder_minutes, existing=f)
    _schedule_follow_up(f, start_at, end_at, reminder)
    f.note = note.strip()[:500] or None
    db.commit()
    realtime.notify_task(task.id)
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/done")
def done_follow_up(
    task_id: str,
    follow_up_id: str,
    request: Request,
    comment: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Marca el seguimiento como hecho y lo deja asentado en los comentarios."""
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    if f.done_at:
        return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)
    f.done_at = datetime.utcnow()
    f.done_by = current_user.id
    view = _follow_up_view(f)
    content = f"✔ Seguimiento del {view['label']} hecho."
    if comment.strip():
        content += f"\n{comment.strip()}"
    db.add(TaskComment(id=uuid.uuid4(), task_id=task.id, user_id=current_user.id, content=content))
    db.commit()

    actor = current_user.name or current_user.email
    for uid in _followers(task):
        if uid and str(uid) != str(current_user.id):
            realtime.notify_user(
                uid,
                title=f"{actor} dio seguimiento",
                body=f"{task.title} · {view['label']}",
                url=f"/tasks/{task.id}",
                kind="task_follow_up",
            )
    realtime.notify_task(task.id)
    audit.log_action(
        "task_follow_up_done", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=view["label"],
    )
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/undo")
def undo_follow_up(
    task_id: str,
    follow_up_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    f.done_at = f.done_by = None
    db.commit()
    realtime.notify_task(task.id)
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


@router.post("/{task_id}/follow-ups/{follow_up_id}/delete")
def delete_follow_up(
    task_id: str,
    follow_up_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    task = _follow_up_task(task_id, current_user, db)
    f = _task_follow_up(task, follow_up_id, db)
    label = _follow_up_view(f)["label"]
    db.delete(f)
    db.commit()
    realtime.notify_task(task.id)
    audit.log_action(
        "task_follow_up_delete", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title, details=label,
    )
    return RedirectResponse(f"/tasks/{task.id}#seguimientos", status_code=302)


# ══════════════════════════════════════════════════════════════════════════════
# Task tags (solo superadmin las crea; siempre por departamento)
# ══════════════════════════════════════════════════════════════════════════════

tags_router = APIRouter(prefix="/tasks/tags", tags=["task-tags"])


@tags_router.get("/", response_class=HTMLResponse)
def list_tags(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    departments = db.query(Department).order_by(Department.name).all()
    tags = db.query(TaskTag).order_by(TaskTag.name).all()
    return templates.TemplateResponse(
        request, "tasks/tags.html",
        {
            "current_user": current_user,
            "departments": departments,
            "tags": tags,
            "colors": TASK_TAG_COLORS,
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@tags_router.post("/")
def create_tag(
    request: Request,
    name: str = Form(...),
    color: str = Form("gray"),
    department_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    if not db.query(Department).filter(Department.id == department_id).first():
        raise HTTPException(404, "Department not found")

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre no puede estar vacío")
    color = color if color in TASK_TAG_COLORS else "gray"

    if db.query(TaskTag).filter(TaskTag.department_id == department_id, TaskTag.name == name).first():
        raise HTTPException(400, "Ya existe una etiqueta con ese nombre en este departamento")

    tag = TaskTag(
        id=uuid.uuid4(), name=name, color=color,
        department_id=department_id, created_by=current_user.id,
    )
    db.add(tag)
    db.commit()
    audit.log_action(
        "task_tag_create", user=current_user, request=request,
        resource_type="task_tag", resource_id=tag.id, resource_name=tag.name,
    )
    return RedirectResponse("/tasks/tags/", status_code=302)


@tags_router.post("/{tag_id}/edit")
def edit_tag(
    tag_id: str,
    request: Request,
    name: str = Form(...),
    color: str = Form("gray"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    tag = db.query(TaskTag).filter(TaskTag.id == tag_id).first()
    if not tag:
        raise HTTPException(404)

    name = name.strip()
    if not name:
        raise HTTPException(400, "El nombre no puede estar vacío")
    dup = db.query(TaskTag).filter(
        TaskTag.department_id == tag.department_id, TaskTag.name == name, TaskTag.id != tag.id,
    ).first()
    if dup:
        raise HTTPException(400, "Ya existe una etiqueta con ese nombre en este departamento")

    tag.name = name
    tag.color = color if color in TASK_TAG_COLORS else "gray"
    db.commit()
    audit.log_action(
        "task_tag_update", user=current_user, request=request,
        resource_type="task_tag", resource_id=tag.id, resource_name=tag.name,
    )
    return RedirectResponse("/tasks/tags/", status_code=302)


@tags_router.post("/{tag_id}/delete")
def delete_tag(
    tag_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if current_user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)

    tag = db.query(TaskTag).filter(TaskTag.id == tag_id).first()
    if not tag:
        raise HTTPException(404)

    name = tag.name
    db.delete(tag)
    db.commit()
    audit.log_action(
        "task_tag_delete", user=current_user, request=request,
        resource_type="task_tag", resource_id=tag_id, resource_name=name,
    )
    return RedirectResponse("/tasks/tags/", status_code=302)


# ══════════════════════════════════════════════════════════════════════════════
# Recurring task templates (predefinidas por el encargado de cada área)
# ══════════════════════════════════════════════════════════════════════════════

recurring_router = APIRouter(prefix="/tasks/recurring", tags=["recurring-tasks"])


def _assignable_department_ids(user: User, db: Session) -> set | None:
    """Departamentos que el encargado puede fijar en una plantilla.

    None = sin restricción (superadmin). Un admin acotado a sucursales
    (Comercial) fija su propio departamento."""
    scope = _manager_scope(user, db)
    if scope is None:
        return None
    dept_ids = set(scope["dept_ids"])
    if scope["branch_dept_id"]:
        dept_ids.add(scope["branch_dept_id"])
    return dept_ids


def _recurring_query(user: User, db: Session):
    q = db.query(RecurringTask).options(
        joinedload(RecurringTask.assignee),
        joinedload(RecurringTask.department),
        joinedload(RecurringTask.project),
        joinedload(RecurringTask.document),
        joinedload(RecurringTask.created_by_user),
        joinedload(RecurringTask.shares).joinedload(RecurringTaskShare.user),
    )
    scope = _manager_scope(user, db)
    if scope is None:
        return q  # superadmin ve todo
    conditions = [RecurringTask.created_by == user.id]
    if scope["dept_ids"]:
        conditions.append(RecurringTask.department_id.in_(scope["dept_ids"]))
    branch_user_ids = _branch_user_ids(scope, db)
    if branch_user_ids:
        conditions.append(RecurringTask.assigned_to.in_(branch_user_ids))
    return q.filter(or_(*conditions))


def _can_manage_recurring(user: User, rt: RecurringTask, db: Session) -> bool:
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(rt.created_by) == str(user.id):
        return True
    scope = _manager_scope(user, db)
    if scope is None:
        return True
    if scope["dept_ids"] and rt.department_id and rt.department_id in scope["dept_ids"]:
        return True
    if rt.assigned_to and rt.assigned_to in _branch_user_ids(scope, db):
        return True
    return False


def _set_recurring_shares(rt: RecurringTask, user_ids: list[str], db: Session) -> None:
    """Deja como compartidos de la plantilla exactamente a `user_ids` (activos;
    sin el creador ni el asignado, que ya siguen cada tarea generada)."""
    try:
        wanted = {uuid.UUID(uid) for uid in user_ids if uid}
    except ValueError:
        raise HTTPException(400, "Persona inválida")
    wanted -= {rt.created_by, uuid.UUID(str(rt.assigned_to)) if rt.assigned_to else None}
    valid = {
        row[0] for row in db.query(User.id).filter(User.id.in_(wanted), User.is_active == True).all()  # noqa: E712
    } if wanted else set()
    current = {s.user_id: s for s in rt.shares}
    for uid, share in current.items():
        if uid not in valid:
            rt.shares.remove(share)
    for uid in valid - set(current):
        rt.shares.append(RecurringTaskShare(user_id=uid))


def _assignable_users_query(user: User, db: Session):
    """Usuarios activos a los que se puede asignar una tarea puntual (persona a persona).

    superadmin → cualquiera. encargado → él mismo y las personas de su alcance
    (manager_scope: su departamento —en Comercial, la gente de sus sucursales—
    y los departamentos con acceso `manage`). employee → sólo él mismo
    (autoasignación)."""
    q = db.query(User).filter(User.is_active == True)
    if user.role == ROLE_SUPERADMIN:
        return q.order_by(User.email)
    if is_manager(user):
        in_scope = scope_user_condition(_manager_scope(user, db))
        if in_scope is None:
            return q.filter(User.id == user.id)
        return q.filter(or_(User.id == user.id, in_scope)).order_by(User.email)
    return q.filter(User.id == user.id)


def _validate_target_scope(
    user: User, db: Session, department_id: str, assigned_to: str, *, required: bool = True,
) -> None:
    """Valida a quién puede dirigir un encargado (o un empleado) una tarea/plantilla.

    - superadmin: sin restricción.
    - encargado (admin, o con acceso `manage`): puede difundir a cualquier
      departamento (`department_id`) sin restricción — es una bandeja compartida
      global, sin elegir persona. Si además señala a alguien puntual
      (`assigned_to`), debe caer dentro de su alcance de gestión — ver
      `_assignable_users_query`.
    - employee: no puede difundir a un departamento ni asignar a otra persona;
      sólo puede autoasignarse (o dejarla sin persona puntual).

    `required`: True exige que venga department_id o assigned_to (plantillas
    recurrentes, que no tienen sentido sin destinatario); False lo permite
    vacío (tarea suelta personal, sin depto ni asignado).
    """
    if user.role == ROLE_SUPERADMIN:
        return
    if required and not department_id and not assigned_to:
        raise HTTPException(400, "Elige un departamento o una persona")

    if is_manager(user):
        if assigned_to:
            allowed_users = {str(u.id) for u in _assignable_users_query(user, db).all()}
            if assigned_to not in allowed_users:
                raise HTTPException(403, "Solo puedes asignar a alguien dentro de tu alcance")
        return

    # employee (o cualquier rol no-encargado): sólo autoasignación
    if department_id:
        raise HTTPException(403, "No puedes asignar tareas a un departamento")
    if assigned_to and assigned_to != str(user.id):
        raise HTTPException(403, "Solo puedes asignarte tareas a ti mismo")


@recurring_router.get("/", response_class=HTMLResponse)
def list_recurring(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    if not is_manager(current_user):
        raise HTTPException(403)

    items = _recurring_query(current_user, db).order_by(RecurringTask.created_at.desc()).all()

    # Proyectos: acotados al alcance de gestión (zona/departamento), igual que
    # antes. Departamentos (difusión) y usuarios (asignación puntual) siguen el
    # criterio nuevo: cualquier encargado difunde a cualquier depto, pero sólo
    # asigna en persona dentro del suyo propio (ver _assignable_users_query).
    dept_ids = _assignable_department_ids(current_user, db)
    proj_q = db.query(Project).order_by(Project.name)
    if dept_ids is not None:
        scoped = dept_ids or {None}  # evita IN () vacío
        proj_q = proj_q.filter(Project.department_id.in_(scoped))

    departments = db.query(Department).order_by(Department.name).all()
    users = _assignable_users_query(current_user, db).all()
    projects = proj_q.all()

    return templates.TemplateResponse(
        request,
        "tasks/recurring.html",
        {
            "current_user": current_user,
            "items": items,
            "departments": departments,
            "users": users,
            "projects": projects,
            "documents": _accessible_documents(current_user),
            # Incluye al propio encargado: puede editar una plantilla que otro
            # compartió con él, y el selector solo conserva a quien conoce.
            "share_candidates": sorted(
                _share_candidates(current_user, db) + [current_user], key=lambda u: (u.name or "", u.email),
            ),
            "priorities": TASK_PRIORITIES,
            "frequencies": RECURRENCE_FREQUENCIES,
            "is_superadmin": current_user.role == ROLE_SUPERADMIN,
            "today": date.today().isoformat(),
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )


@recurring_router.post("/")
def create_recurring(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_id: str = Form(""),
    document_id: str = Form(""),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    start_date: str = Form(""),
    end_date: str = Form(""),
    shared_with: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not is_manager(current_user):
        raise HTTPException(403)

    _validate_target_scope(current_user, db, department_id, assigned_to)
    freq, dow, dom, custom_days = parse_recurrence(frequency, day_of_week, day_of_month, days_of_week)

    rt = RecurringTask(
        id=uuid.uuid4(),
        title=title.strip(),
        description=description.strip() or None,
        priority=priority if priority in TASK_PRIORITIES else "medium",
        department_id=department_id if department_id else None,
        assigned_to=assigned_to if assigned_to else None,
        project_id=project_id if project_id else None,
        document_id=_validated_document_id(document_id, current_user, db),
        created_by=current_user.id,
        frequency=freq,
        day_of_week=dow,
        day_of_month=dom,
        days_of_week=custom_days,
        start_date=date.fromisoformat(start_date) if start_date else None,
        end_date=date.fromisoformat(end_date) if end_date else None,
    )
    _set_recurring_shares(rt, shared_with, db)
    db.add(rt)
    db.commit()
    audit.log_action(
        "recurring_create", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt.id, resource_name=rt.title,
        details=f"frequency={rt.frequency}",
    )
    return RedirectResponse("/tasks/recurring/", status_code=302)


@recurring_router.post("/{rt_id}/edit")
def edit_recurring(
    rt_id: str,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_id: str = Form(""),
    document_id: str = Form(""),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    start_date: str = Form(""),
    end_date: str = Form(""),
    shared_with: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)

    rt = db.query(RecurringTask).filter(RecurringTask.id == rt_id).first()
    if not rt:
        raise HTTPException(404)
    if not _can_manage_recurring(current_user, rt, db):
        raise HTTPException(403)

    _validate_target_scope(current_user, db, department_id, assigned_to)
    freq, dow, dom, custom_days = parse_recurrence(frequency, day_of_week, day_of_month, days_of_week)

    rt.title = title.strip()
    rt.description = description.strip() or None
    rt.priority = priority if priority in TASK_PRIORITIES else "medium"
    rt.department_id = department_id if department_id else None
    rt.assigned_to = assigned_to if assigned_to else None
    rt.project_id = project_id if project_id else None
    rt.document_id = _validated_document_id(document_id, current_user, db)
    rt.frequency = freq
    rt.day_of_week = dow
    rt.day_of_month = dom
    rt.days_of_week = custom_days
    rt.start_date = date.fromisoformat(start_date) if start_date else None
    rt.end_date = date.fromisoformat(end_date) if end_date else None
    _set_recurring_shares(rt, shared_with, db)
    db.commit()
    audit.log_action(
        "recurring_update", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt.id, resource_name=rt.title,
    )
    return RedirectResponse("/tasks/recurring/", status_code=302)


@recurring_router.post("/{rt_id}/toggle", response_class=HTMLResponse)
def toggle_recurring(
    rt_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    rt = db.query(RecurringTask).filter(RecurringTask.id == rt_id).first()
    if not rt:
        raise HTTPException(404)
    if not _can_manage_recurring(current_user, rt, db):
        raise HTTPException(403)
    rt.is_active = not rt.is_active
    db.commit()
    audit.log_action(
        "recurring_toggle", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt.id, resource_name=rt.title,
        details=f"is_active={rt.is_active}",
    )
    return HTMLResponse(headers={"HX-Redirect": "/tasks/recurring/"})


@recurring_router.post("/{rt_id}/delete", response_class=HTMLResponse)
def delete_recurring(
    rt_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    rt = db.query(RecurringTask).filter(RecurringTask.id == rt_id).first()
    if not rt:
        raise HTTPException(404)
    if not _can_manage_recurring(current_user, rt, db):
        raise HTTPException(403)
    title = rt.title
    db.delete(rt)
    db.commit()
    audit.log_action(
        "recurring_delete", user=current_user, request=request,
        resource_type="recurring_task", resource_id=rt_id, resource_name=title,
    )
    return HTMLResponse(headers={"HX-Redirect": "/tasks/recurring/"})
