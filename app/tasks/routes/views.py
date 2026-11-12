"""Listado, archivadas, detalle y creación de tareas, y sus streams SSE."""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from jose import JWTError
from sqlalchemy import false, or_
from sqlalchemy.orm import Session, joinedload

from app.auth.deps import get_current_user
from app.auth.utils import decode_token, generate_csrf_token
from app.database import get_db, SessionLocal
from app import audit, reactions, storage, images
from app.documents.router import _preview_meta
from app.messaging import realtime
from app.permissions import (
    granted_department_ids,
    has_oversight,
    is_manager,
    branch_user_ids as _branch_user_ids,
    manager_scope as _manager_scope,
    scope_user_condition,
)
from app.models import (
    Department,
    Project,
    Task,
    TaskComment,
    TaskCommentReaction,
    TaskEvidence,
    TaskFollowUp,
    TaskShare,
    TaskTag,
    TaskTagAssignment,
    User,
    CALENDAR_REMINDER_OPTIONS,
    ROLE_AUDITOR,
    TASK_STATUSES,
    TASK_PRIORITIES,
)
from app.tasks.recurring import local_now
from app.tasks import service as task_service
from app.tasks.service import (
    _assignable_users_query,
    _can_edit_task,
    _can_manage_follow_ups,
    _can_view_task,
    _tasks_query,
)
from app.templating import templates
from app.tasks.helpers import (
    _accessible_documents,
    _add_shares,
    _can_approve_task,
    _can_comment,
    _can_share,
    _can_update_status,
    _comment_threads,
    _follow_up_view,
    _notify_shared,
    _parse_date,
    _safe_filename,
    _share_candidates,
    _status_durations,
    _validated_tag_ids,
)

router = APIRouter()


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
        joinedload(Task.projects),
        joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
        joinedload(Task.shares).joinedload(TaskShare.user),
    ).filter(Task.archived_at.is_(None))

    # Data for create form. "departments" es difusión abierta (cualquier admin
    # puede difundir a cualquier departamento); "users" es asignación puntual,
    # acotada al alcance de cada rol (ver _assignable_users_query).
    departments = db.query(Department).order_by(Department.name).all()
    users = _assignable_users_query(current_user, db).all()
    projects = task_service.visible_projects(current_user, db).order_by(Project.name).all()
    tags = db.query(TaskTag).order_by(TaskTag.name).all()

    # Filtro por departamento en las vistas personales y para superadmin.
    # En la vista Departamento, los administradores filtran por persona
    # dentro de su alcance de gestión.
    filter_departments = []
    filter_users = []
    filter_dept_id = ""
    filter_user_id = ""
    # Departamento: tareas que entran solo por estar compartidas con alguien del
    # alcance (no son del departamento); llevan la etiqueta "Compartida".
    is_own = None

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
            # Las compartidas con alguien de su alcance, aunque sean de otro departamento.
            shared_cond = task_service.shared_with_overseen_condition(current_user, db)
            if shared_cond is not None:
                conds.append(shared_cond)
            q = base.filter(or_(*conds)) if conds else base.filter(false())
            is_own = lambda t: t.department_id in dept_ids or t.assigned_to in branch_user_ids  # noqa: E731

            # Filtro por persona: las de su alcance y las de sus departamentos
            # con acceso extra.
            user_q = db.query(User).filter(User.is_active == True)
            in_scope = scope_user_condition({**scope, "dept_ids": dept_ids})
            user_q = user_q.filter(in_scope if in_scope is not None else false())
            filter_users = user_q.order_by(User.email).all()
            if user_id and any(str(u.id) == user_id for u in filter_users):
                q = q.filter(or_(Task.assigned_to == user_id, Task.shares.any(TaskShare.user_id == user_id)))
                filter_user_id = user_id
        can_drag = False

    if tab in ("assigned", "created", "shared", "all"):
        filter_departments = departments
    selected_department = next((d for d in filter_departments if str(d.id) == dept_id), None)
    if selected_department is not None:
        dept_cond = Task.department_id == selected_department.id
        if tab == "dept":  # superadmin: también las compartidas con gente de ese departamento
            dept_cond = or_(dept_cond, Task.shares.any(
                TaskShare.user.has(User.department_id == selected_department.id)
            ))
            is_own = lambda t: t.department_id == selected_department.id  # noqa: E731
        q = q.filter(dept_cond)
        filter_dept_id = dept_id

    search = search.strip()
    if search:
        q = q.filter(or_(
            Task.title.icontains(search, autoescape=True),
            Task.description.icontains(search, autoescape=True),
        ))
    tasks = q.order_by(Task.created_at.desc()).all()
    shared_tag_ids = {t.id for t in tasks if not is_own(t)} if tab == "dept" and is_own else set()

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
            "shared_tag_ids": shared_tag_ids,
            "statuses": TASK_STATUSES,
            "priorities": TASK_PRIORITIES,
            "today": date.today().isoformat(),
            "csrf_token": csrf,
        },
    )


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


@router.post("/")
def create_task(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    priority: str = Form("medium"),
    status: str = Form("pending"),
    department_id: str = Form(""),
    assigned_to: str = Form(""),
    project_ids: list[str] = Form(default=[]),
    document_id: str = Form(""),
    due_date: str = Form(""),
    start_date: str = Form(""),
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

    task = task_service.create_task(
        db, current_user,
        title=title,
        description=description,
        priority=priority,
        status=status,
        department_id=department_id,
        assigned_to=assigned_to,
        project_ids=project_ids,
        document_id=document_id,
        due_date=_parse_date(due_date, "Fecha límite inválida"),
        start_date=_parse_date(start_date, "Fecha de inicio inválida"),
    )
    for tid in _validated_tag_ids(tag_ids, task.department_id, db):
        db.add(TaskTagAssignment(task_id=task.id, tag_id=tid))
    shared_ids = _add_shares(task, shared_with, current_user, db)

    for f in evidences:
        if not f.filename:
            continue
        content, filename, ctype = images.optimize_image(
            f.file.read(), f.filename, f.content_type or "application/octet-stream",
        )
        safe = _safe_filename(filename)
        key = f"tasks/{task.id}/{uuid.uuid4()}_{safe}"
        storage.upload_evidence(key, content, ctype)
        db.add(TaskEvidence(
            id=uuid.uuid4(),
            task_id=task.id,
            uploaded_by=current_user.id,
            filename=filename,
            file_key=key,
            content_type=ctype,
            file_size=len(content),
        ))

    db.commit()
    task_service.notify_task_created(task, current_user)
    _notify_shared(task, shared_ids, current_user)
    audit.log_action(
        "task_create", user=current_user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"priority={task.priority} evidences={len([f for f in evidences if f.filename])} shared={len(shared_ids)}",
    )
    redirect = next_url if next_url and next_url.startswith("/") else "/tasks/"
    return RedirectResponse(redirect, status_code=302)


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
            joinedload(Task.projects),
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
    if not in_scope and not _can_view_task(current_user, task_id, db):
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
    can_edit = _can_edit_task(current_user, task, db)
    has_gantt = task_service.has_gantt(task)
    project_options = (
        task_service.visible_projects(current_user, db).order_by(Project.name).all() if can_edit else []
    )

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
            "can_edit": can_edit,
            "project_options": project_options,
            "selected_project_ids": {str(p.id) for p in task.projects},
            "has_gantt": has_gantt,
            "is_blocked": has_gantt and task_service.is_blocked(task),
            "dependency_candidates": task_service.dependency_candidates(task, db) if has_gantt and can_edit else [],
            "can_update_status": _can_update_status(current_user, task, db),
            "is_admin": is_manager(current_user),
            "can_approve": _can_approve_task(current_user, task),
            "can_participate": can_participate,
            "can_comment": _can_comment(current_user, task_id, db),
            "comment_threads": _comment_threads(task.comments),
            # Consulta agrupada aparte: otro joinedload multiplicaría las filas de la de arriba.
            "comment_reactions": reactions.summarize(
                db, TaskCommentReaction, [c.id for c in task.comments], current_user.id,
            ),
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
