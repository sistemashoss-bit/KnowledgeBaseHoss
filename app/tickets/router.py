"""Tickets: incidencias y solicitudes a un departamento.

Se identifican por su folio (`Ticket.number`), que es lo que va en la URL:
/tickets/1024. Comentarios (hilos, stickers, reacciones) y evidencias reutilizan
las plantillas de tareas con `comment_base`/`ev_base` apuntando aquí, y la
lógica común de app/comments.py y app/attachments.py.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from jwt import PyJWTError as JWTError
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app import attachments, audit, comments, reactions, storage
from app.auth.deps import get_current_user
from app.auth.utils import decode_token, generate_csrf_token
from app.config import settings
from app.database import SessionLocal, get_db
from app.messaging import realtime
from app.models import (
    Department, Ticket, TicketCategory, TicketCategoryStep, TicketComment, TicketCommentReaction,
    TicketEvidence, TicketSlaAlert, TicketSlaPolicy, TicketStatusHistory, User,
    ALERT_DEPARTMENT, ALERT_TARGETS, STEP_DEPARTMENT, STEP_TARGETS, STEP_USER,
    PRIORITY_MEDIUM, ROLE_AUDITOR, ROLE_SUPERADMIN, TASK_PRIORITIES,
    TICKET_ACTIVE_STATUSES, TICKET_CLOSED, TICKET_KINDS, TICKET_RESOLVED, TICKET_STATUSES,
)
from app.permissions import managed_department_ids, overseen_department_ids
from app.tasks.business_hours import business_hours_label
from app.templating import templates
from app.tickets import service as svc
from app.tickets.categories import (
    ALERT_LABELS, TARGET_LABELS, alert_label, escalate, escalation_recipients, has_next_step, step_label,
)

router = APIRouter(prefix="/tickets", tags=["tickets"])

PAGE_SIZE = 25
STATUS_LABELS = {
    "open": "Abierto", "in_progress": "En atención", "waiting": "En espera",
    "resolved": "Resuelto", "closed": "Cerrado",
}
KIND_LABELS = {"incident": "Incidencia", "request": "Solicitud"}
PRIORITY_LABELS = {"urgent": "Urgente", "high": "Alta", "medium": "Media", "low": "Baja"}
STATUS_GROUPS = {
    "active": TICKET_ACTIVE_STATUSES,
    "resolved": [TICKET_RESOLVED],
    "closed": [TICKET_CLOSED],
    "all": TICKET_STATUSES,
}


def _url(t: Ticket) -> str:
    return f"/tickets/{t.number}"


def _label(t: Ticket) -> str:
    return f"#{t.number} {t.title}"


def _get(number: int, db: Session, user: User) -> Ticket:
    t = db.query(Ticket).filter(Ticket.number == number).first()
    if not t:
        raise HTTPException(404, "El ticket no existe")
    if not svc.can_view(user, t):
        raise HTTPException(403)
    return t


def _require_user(user):
    if not user:
        raise HTTPException(401)
    return user


def _notify(user_ids, actor: User, title: str, t: Ticket, kind: str, url: str | None = None) -> None:
    ids = [uid for uid in set(user_ids) if uid and str(uid) != str(actor.id)]
    if ids:
        realtime.notify_users(ids, title=title, body=_label(t), url=url or _url(t), kind=kind)


def _actor(user: User) -> str:
    return user.name or user.email


def _save_files(t: Ticket, user: User, files: list[UploadFile], db: Session) -> list[str]:
    stored = attachments.store_uploads(f"tickets/{t.id}", files)
    for f in stored:
        db.add(TicketEvidence(id=uuid.uuid4(), ticket_id=t.id, uploaded_by=user.id, **f))
    return [f["filename"] for f in stored]


# ── Lista ─────────────────────────────────────────────────────────────────────

def _default_view(user: User, db: Session) -> str:
    """Quien atiende tickets (su departamento ya recibió alguno) cae en la cola
    del departamento; el resto, en sus solicitudes."""
    if user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR):
        return "all"
    depts = svc.attended_department_ids(user) | overseen_department_ids(user)
    if depts and db.query(Ticket.id).filter(Ticket.department_id.in_(depts)).first():
        return "dept"
    return "mine"


@router.get("/", response_class=HTMLResponse)
def list_tickets(
    request: Request,
    view: str = "",
    status: str = "active",
    kind: str = "",
    priority: str = "",
    department_id: str = "",
    q: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    user = current_user
    sees_all = user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR)
    dept_ids = svc.attended_department_ids(user) | overseen_department_ids(user)

    views = {"mine": Ticket.requester_id == user.id, "assigned": Ticket.assigned_to == user.id}
    if dept_ids:
        views["dept"] = Ticket.department_id.in_(dept_ids)
    if sees_all:
        views["all"] = None
    view = view if view in views else _default_view(user, db)
    if view not in views:
        view = "mine"

    base = db.query(Ticket)
    visible = svc.visible_condition(user)
    if visible is not None:
        base = base.filter(visible)

    # Activos por pestaña, para los contadores.
    counts = {}
    for name, cond in views.items():
        cq = base.filter(Ticket.status.in_(TICKET_ACTIVE_STATUSES))
        counts[name] = (cq.filter(cond) if cond is not None else cq).count()

    query = base.filter(views[view]) if views[view] is not None else base
    status = status if status in STATUS_GROUPS else "active"
    query = query.filter(Ticket.status.in_(STATUS_GROUPS[status]))
    if kind in TICKET_KINDS:
        query = query.filter(Ticket.kind == kind)
    if priority in TASK_PRIORITIES:
        query = query.filter(Ticket.priority == priority)

    filter_departments = []
    if view in ("dept", "all"):
        filter_departments = (
            db.query(Department).order_by(Department.name).all() if view == "all"
            else db.query(Department).filter(Department.id.in_(dept_ids)).order_by(Department.name).all()
        )
        if department_id and department_id in {str(d.id) for d in filter_departments}:
            query = query.filter(Ticket.department_id == uuid.UUID(department_id))
        else:
            department_id = ""
    else:
        department_id = ""

    q = q.strip()
    if q:
        number = q.lstrip("#").strip()
        query = query.filter(
            Ticket.number == int(number) if number.isdigit() and len(number) < 10
            else Ticket.title.ilike(f"%{q}%")
        )

    total = query.count()
    page = max(1, page)
    tickets = (
        query.options(
            joinedload(Ticket.requester), joinedload(Ticket.assignee), joinedload(Ticket.department),
            selectinload(Ticket.status_history),
        )
        .order_by(Ticket.created_at.desc())
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE).all()
    )
    now = datetime.utcnow()

    return templates.TemplateResponse(request, "tickets/list.html", {
        "current_user": user,
        "tickets": tickets,
        "sla": {t.id: svc.sla_status(t, now) for t in tickets},
        "view": view, "views": list(views), "counts": counts,
        "status": status, "kind": kind, "priority": priority, "department_id": department_id, "q": q,
        "filter_departments": filter_departments,
        "page": page, "total": total, "page_size": PAGE_SIZE,
        "status_labels": STATUS_LABELS, "kind_labels": KIND_LABELS, "priority_labels": PRIORITY_LABELS,
        "priorities": TASK_PRIORITIES,
        "can_edit_sla": user.role == ROLE_SUPERADMIN or bool(managed_department_ids(user)),
    })


# ── Nuevo ─────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
def new_ticket(
    request: Request,
    kind: str = "incident",
    department_id: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    return templates.TemplateResponse(request, "tickets/new.html", {
        "current_user": current_user,
        "departments": db.query(Department).order_by(Department.name).all(),
        "kind": kind if kind in TICKET_KINDS else "incident",
        "department_id": department_id,
        "priorities": TASK_PRIORITIES, "priority_labels": PRIORITY_LABELS,
        "sla_hours": settings.ticket_sla_hours,
        "csrf_token": generate_csrf_token(str(current_user.id)),
    })


@router.post("/")
def create_ticket(
    request: Request,
    kind: str = Form("incident"),
    title: str = Form(...),
    description: str = Form(""),
    department_id: str = Form(""),
    priority: str = Form(PRIORITY_MEDIUM),
    evidences: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    storage.check_upload_sizes(*evidences)
    t = svc.create_ticket(
        db, user, kind=kind, title=title, description=description,
        department_id=department_id, priority=priority,
    )
    names = _save_files(t, user, evidences, db)
    db.commit()

    _notify(
        svc.department_admin_ids(t.department_id, db), user,
        f"Nuevo ticket de {_actor(user)}", t, "ticket_created",
    )
    audit.log_action(
        "ticket_create", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=_label(t),
        details=f"kind={t.kind} priority={t.priority} evidences={len(names)}",
    )
    return RedirectResponse(_url(t), status_code=302)


# ── SLA por departamento ─────────────────────────────────────────────────────

def _sla_departments(user: User, db: Session) -> list[Department]:
    q = db.query(Department).order_by(Department.name)
    if user.role != ROLE_SUPERADMIN:
        ids = managed_department_ids(user)
        if not ids:
            return []
        q = q.filter(Department.id.in_(ids))
    return q.all()


def _hours(minutes: int | None) -> str:
    if minutes is None:
        return ""
    h = minutes / 60
    return str(int(h)) if h == int(h) else f"{h:g}"


@router.get("/sla", response_class=HTMLResponse)
def sla_settings(
    request: Request,
    department_id: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    departments = _sla_departments(current_user, db)
    if not departments:
        raise HTTPException(403)
    dept = next((d for d in departments if str(d.id) == department_id), departments[0])
    rows = {p.priority: p for p in db.query(TicketSlaPolicy).filter(TicketSlaPolicy.department_id == dept.id)}
    defaults = settings.ticket_sla_hours
    policy = []
    for p in TASK_PRIORITIES[::-1]:
        row = rows.get(p)
        policy.append({
            "priority": p,
            "custom": row is not None,
            "response": _hours(row.response_minutes) if row else "",
            "resolution": _hours(row.resolution_minutes) if row else "",
            "default_response": f"{defaults[p][0]:g}" if p in defaults else "",
            "default_resolution": f"{defaults[p][1]:g}" if p in defaults else "",
        })
    return templates.TemplateResponse(request, "tickets/sla.html", {
        "current_user": current_user,
        "departments": departments, "dept": dept, "policy": policy,
        "priority_labels": PRIORITY_LABELS,
        "hours_label": business_hours_label(),
        "saved": request.query_params.get("saved") == "1",
        "csrf_token": generate_csrf_token(str(current_user.id)),
    })


def _parse_hours(raw: str) -> int | None:
    raw = (raw or "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise HTTPException(400, "Escribe las horas como número (p. ej. 4 o 0.5)")
    if value <= 0 or value > 2000:
        raise HTTPException(400, "Las horas deben estar entre 0 y 2000")
    return round(value * 60)


@router.post("/sla")
async def save_sla(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    form = await request.form()
    department_id = str(form.get("department_id") or "")
    dept = next((d for d in _sla_departments(user, db) if str(d.id) == department_id), None)
    if not dept:
        raise HTTPException(403)
    for p in TASK_PRIORITIES:
        response = _parse_hours(str(form.get(f"response_{p}") or ""))
        resolution = _parse_hours(str(form.get(f"resolution_{p}") or ""))
        row = db.get(TicketSlaPolicy, (dept.id, p))
        if response is None and resolution is None:
            # Vacío = vuelve al default global.
            if row:
                db.delete(row)
            continue
        if response and resolution and response > resolution:
            raise HTTPException(400, "La primera respuesta no puede tardar más que la resolución")
        if not row:
            row = TicketSlaPolicy(department_id=dept.id, priority=p)
            db.add(row)
        row.response_minutes, row.resolution_minutes = response, resolution
    db.commit()
    audit.log_action(
        "ticket_sla_update", user=user, request=request,
        resource_type="department", resource_id=dept.id, resource_name=dept.name,
    )
    return RedirectResponse(f"/tickets/sla?department_id={dept.id}&saved=1", status_code=302)


# ── Categorías (Comercial, PLAN_CATEGORIAS_TICKETS.md) ───────────────────────

CATEGORY_BLANK_STEPS = 3


def _require_superadmin(user):
    if not user or user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)
    return user


def _category_departments(db: Session) -> list[Department]:
    """Los acotados por sucursal (Comercial) primero: son los que hoy usan categorías."""
    return sorted(db.query(Department).all(), key=lambda d: (not d.branch_scoped, d.name.lower()))


@router.get("/categorias", response_class=HTMLResponse)
def categories_page(
    request: Request,
    department_id: str = "",
    edit: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    user = _require_superadmin(current_user)
    departments = _category_departments(db)
    if not departments:
        raise HTTPException(400, "No hay departamentos")
    dept = next((d for d in departments if str(d.id) == department_id), departments[0])
    categories = (
        db.query(TicketCategory)
        .options(selectinload(TicketCategory.steps).joinedload(TicketCategoryStep.department),
                 selectinload(TicketCategory.steps).joinedload(TicketCategoryStep.user),
                 selectinload(TicketCategory.steps).joinedload(TicketCategoryStep.alert_department))
        .filter(TicketCategory.department_id == dept.id)
        .order_by(TicketCategory.sort_order, TicketCategory.name).all()
    )
    editing = None
    if edit == "new":
        editing = TicketCategory(department_id=dept.id, default_kind=TICKET_KINDS[0], sort_order=len(categories), active=True)
    elif edit:
        editing = next((c for c in categories if str(c.id) == edit), None)
    step_rows = []
    if editing is not None:
        for st in (editing.steps or []):
            step_rows.append({
                "target": st.target, "department_id": str(st.department_id or ""), "user_id": str(st.user_id or ""),
                "response": _hours(st.sla_response_minutes), "resolution": _hours(st.sla_resolution_minutes),
                "alert": st.alert_target or "", "alert_department_id": str(st.alert_department_id or ""),
            })
        step_rows += [{
            "target": "", "department_id": "", "user_id": "", "response": "", "resolution": "",
            "alert": "", "alert_department_id": "",
        }] * CATEGORY_BLANK_STEPS
    people = (
        db.query(User).filter(User.is_active == True, User.is_bot == False)  # noqa: E712
        .order_by(User.name, User.email).all() if editing is not None else []
    )
    return templates.TemplateResponse(request, "tickets/categories.html", {
        "current_user": user,
        "departments": departments, "dept": dept, "categories": categories,
        "editing": editing, "step_rows": step_rows, "people": people,
        "all_departments": sorted(departments, key=lambda d: d.name.lower()),
        "target_labels": TARGET_LABELS, "step_label": step_label,
        "alert_labels": ALERT_LABELS, "alert_label": alert_label,
        "kind_labels": KIND_LABELS, "kinds": TICKET_KINDS,
        "hours_label": business_hours_label(),
        "saved": request.query_params.get("saved") == "1",
        "csrf_token": generate_csrf_token(str(user.id)),
    })


@router.post("/categorias")
async def save_category(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_superadmin(current_user)
    form = await request.form()
    get = lambda k: str(form.get(k) or "").strip()  # noqa: E731
    try:
        dept = db.get(Department, uuid.UUID(get("department_id")))
    except ValueError:
        dept = None
    if not dept:
        raise HTTPException(400, "Departamento inválido")

    category_id = get("category_id")
    if category_id:
        category = db.get(TicketCategory, uuid.UUID(category_id))
        if not category or category.department_id != dept.id:
            raise HTTPException(404, "La categoría no existe")
    else:
        category = TicketCategory(id=uuid.uuid4(), department_id=dept.id)
        db.add(category)

    name = get("name")[:100]
    if not name:
        raise HTTPException(400, "Escribe el nombre de la categoría")
    clash = db.query(TicketCategory.id).filter(
        TicketCategory.department_id == dept.id, func.lower(TicketCategory.name) == name.lower(),
        TicketCategory.id != category.id,
    ).first()
    if clash:
        raise HTTPException(400, "Ya hay una categoría con ese nombre en el departamento")
    kind = get("default_kind")
    if kind not in TICKET_KINDS:
        raise HTTPException(400, "Tipo inválido")
    try:
        sort_order = int(get("sort_order") or 0)
    except ValueError:
        raise HTTPException(400, "El orden debe ser un número")

    steps = []
    for i in range(int(get("step_count") or 0)):
        target = get(f"target_{i}")
        if not target:
            continue
        if target not in STEP_TARGETS:
            raise HTTPException(400, "Tipo de paso inválido")
        step = {"target": target, "department_id": None, "user_id": None}
        n = len(steps) + 1
        if target == STEP_DEPARTMENT:
            try:
                step["department_id"] = uuid.UUID(get(f"department_{i}"))
            except ValueError:
                raise HTTPException(400, f"Elige el departamento del paso {n}")
            if not db.get(Department, step["department_id"]):
                raise HTTPException(400, f"Departamento inválido en el paso {n}")
        elif target == STEP_USER:
            try:
                person = db.get(User, uuid.UUID(get(f"user_{i}")))
            except ValueError:
                person = None
            if not person or not person.is_active:
                raise HTTPException(400, f"Elige una persona activa en el paso {n}")
            step["user_id"] = person.id
        step["response"] = _parse_hours(get(f"response_{i}"))
        step["resolution"] = _parse_hours(get(f"resolution_{i}"))
        if step["response"] and step["resolution"] and step["response"] > step["resolution"]:
            raise HTTPException(400, f"En el paso {n} la primera respuesta no puede tardar más que la resolución")
        alert = get(f"alert_{i}")
        if alert and alert not in ALERT_TARGETS:
            raise HTTPException(400, f"Aviso inválido en el paso {n}")
        if alert and not (step["response"] or step["resolution"]):
            raise HTTPException(400, f"El paso {n} avisa si se vence el SLA, pero no tiene SLA")
        step["alert"], step["alert_department_id"] = alert or None, None
        if alert == ALERT_DEPARTMENT:
            try:
                step["alert_department_id"] = uuid.UUID(get(f"alert_department_{i}"))
            except ValueError:
                raise HTTPException(400, f"Elige el departamento al que se avisa en el paso {n}")
            if not db.get(Department, step["alert_department_id"]):
                raise HTTPException(400, f"Departamento de aviso inválido en el paso {n}")
        steps.append(step)
    if not steps:
        raise HTTPException(400, "La categoría necesita al menos un paso")

    category.name = name
    category.description = get("description")[:2000] or None
    category.default_kind = kind
    category.sort_order = sort_order
    category.active = form.get("active") == "on"
    # Los pasos se reemplazan; los tickets en curso conservan su número de paso.
    db.query(TicketCategoryStep).filter(TicketCategoryStep.category_id == category.id).delete()
    db.flush()
    for n, st in enumerate(steps, start=1):
        db.add(TicketCategoryStep(
            id=uuid.uuid4(), category_id=category.id, position=n, target=st["target"],
            department_id=st["department_id"], user_id=st["user_id"],
            sla_response_minutes=st["response"], sla_resolution_minutes=st["resolution"],
            alert_target=st["alert"], alert_department_id=st["alert_department_id"],
        ))
    db.commit()
    audit.log_action(
        "ticket_category_save", user=user, request=request,
        resource_type="ticket_category", resource_id=category.id, resource_name=f"{dept.name} · {name}",
        details=f"steps={len(steps)} active={category.active}",
    )
    return RedirectResponse(f"/tickets/categorias?department_id={dept.id}&saved=1", status_code=302)


# ── Detalle ───────────────────────────────────────────────────────────────────

@router.get("/{number:int}", response_class=HTMLResponse)
def ticket_detail(
    number: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    user = current_user
    t = (
        db.query(Ticket)
        .options(
            joinedload(Ticket.requester).joinedload(User.department),
            joinedload(Ticket.assignee), joinedload(Ticket.department),
            selectinload(Ticket.comments).joinedload(TicketComment.user),
            selectinload(Ticket.evidences).joinedload(TicketEvidence.uploader),
            selectinload(Ticket.status_history).joinedload(TicketStatusHistory.changed_by_user),
            selectinload(Ticket.status_history).joinedload(TicketStatusHistory.to_department),
            selectinload(Ticket.status_history).joinedload(TicketStatusHistory.to_user),
            selectinload(Ticket.sla_alerts).joinedload(TicketSlaAlert.user),
        )
        .filter(Ticket.number == number).first()
    )
    if not t:
        return RedirectResponse("/tickets/", status_code=302)
    if not svc.can_view(user, t):
        raise HTTPException(403)

    can_attend = svc.can_attend(user, t)
    can_comment = svc.can_comment(user, t)
    step_info = None
    if t.category:
        steps = t.category.steps
        current = next((s for s in steps if s.position == t.step_position), None)
        step_info = {
            "category": t.category.name,
            "position": steps.index(current) + 1 if current else None,
            "total": len(steps),
            "label": step_label(current) if current else None,
        }
    return templates.TemplateResponse(request, "tickets/detail.html", {
        "current_user": user,
        "ticket": t,
        "step_info": step_info,
        "in_direct_boss_step": svc.in_direct_boss_step(t),
        "can_escalate": can_attend and t.status in TICKET_ACTIVE_STATUSES and has_next_step(t),
        "sla": svc.sla_status(t),
        "hours_label": business_hours_label(),
        "is_requester": svc.is_requester(user, t),
        "can_attend": can_attend,
        "can_manage": svc.can_manage(user, t),
        "can_comment": can_comment,
        "can_edit_text": svc.can_edit_text(user, t),
        "can_delete": user.role == ROLE_SUPERADMIN,
        "allowed_statuses": svc.allowed_statuses(user, t),
        "assignable_users": svc.assignable_users(t, db) if can_attend else [],
        "departments": db.query(Department).order_by(Department.name).all() if can_attend else [],
        "comment_threads": comments.threads(t.comments),
        "comment_reactions": reactions.summarize(
            db, TicketCommentReaction, [c.id for c in t.comments], user.id,
        ),
        "status_labels": STATUS_LABELS, "kind_labels": KIND_LABELS, "priority_labels": PRIORITY_LABELS,
        "priorities": TASK_PRIORITIES, "kinds": TICKET_KINDS,
        "csrf_token": generate_csrf_token(str(user.id)),
    })


@router.get("/{number:int}/stream")
async def ticket_stream(number: int, request: Request):
    """SSE del detalle; sesión corta para validar y no retener conexión a la BD."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(401)
    try:
        user_id = decode_token(token).get("sub")
    except JWTError:
        raise HTTPException(401)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first() if user_id else None
        t = db.query(Ticket).filter(Ticket.number == number).first()
        if not user or not t or not svc.can_view(user, t):
            raise HTTPException(403)
        ticket_id = str(t.id)
    finally:
        db.close()
    return StreamingResponse(
        realtime.ticket_event_stream(ticket_id, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Cambios ───────────────────────────────────────────────────────────────────

@router.post("/{number:int}/status")
def update_status(
    number: int,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if status not in svc.allowed_statuses(user, t):
        raise HTTPException(403, "No puedes mover el ticket a ese estado")
    old = t.status
    svc.record_status(t, status, user, db)
    svc.mark_responded(t, user)
    db.commit()

    _notify(
        svc.followers(t, db), user,
        f"{_actor(user)} cambió el ticket a {STATUS_LABELS[status]}", t, "ticket_status",
    )
    realtime.notify_ticket(t.id)
    audit.log_action(
        "ticket_status", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=_label(t), details=f"{old} -> {status}",
    )
    return RedirectResponse(_url(t), status_code=302)


@router.post("/{number:int}/assign")
def assign_ticket(
    number: int,
    request: Request,
    assigned_to: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_attend(user, t):
        raise HTTPException(403)
    target = None
    if assigned_to:
        target = next((u for u in svc.assignable_users(t, db) if str(u.id) == assigned_to), None)
        if not target:
            raise HTTPException(400, "Solo se asigna a alguien activo del departamento que lo atiende")
    elif svc.in_direct_boss_step(t):
        # Sin asignado lo vería todo el departamento de quien reporta (Comercial).
        raise HTTPException(400, "En el paso del jefe directo el ticket siempre tiene responsable")
    t.assigned_to = target.id if target else None
    db.commit()

    if target and target.id != user.id:
        realtime.notify_user(
            target.id, title=f"{_actor(user)} te asignó un ticket", body=_label(t), url=_url(t),
            kind="ticket_assigned",
        )
    realtime.notify_ticket(t.id)
    audit.log_action(
        "ticket_assign", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=_label(t),
        details=f"assigned_to={target.email if target else '-'}",
    )
    return RedirectResponse(_url(t), status_code=302)


@router.post("/{number:int}/escalate")
def escalate_ticket(
    number: int,
    request: Request,
    note: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Pasa un ticket con categoría al siguiente paso (quien lo atiende, con motivo)."""
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_attend(user, t):
        raise HTTPException(403)
    if not t.category_id:
        raise HTTPException(400, "Solo se escalan los tickets con categoría")
    if t.status not in TICKET_ACTIVE_STATUSES:
        raise HTTPException(400, "El ticket ya está resuelto o cerrado")
    if not note.strip():
        raise HTTPException(400, "Escribe por qué lo escalas")
    esc = escalate(t, db, actor=user, note=note)
    if not esc:
        raise HTTPException(400, "Ya no hay más pasos a los que escalarlo")
    db.commit()

    _notify(escalation_recipients(t, db), user, f"{_actor(user)} escaló un ticket", t, "ticket_escalate")
    realtime.notify_ticket(t.id)
    audit.log_action(
        "ticket_escalate", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=_label(t),
        details=f"step {esc.from_position} -> {esc.to_position}",
    )
    # Quien lo escaló deja de verlo si ya no le toca.
    return RedirectResponse(_url(t) if svc.can_view(user, t) else "/tickets/", status_code=302)


@router.post("/{number:int}/transfer")
def transfer_ticket(
    number: int,
    request: Request,
    department_id: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_attend(user, t):
        raise HTTPException(403)
    if t.category_id:
        raise HTTPException(400, "Un ticket con categoría no se transfiere: sigue los pasos de su categoría")
    try:
        dept = db.get(Department, uuid.UUID(department_id))
    except ValueError:
        dept = None
    if not dept:
        raise HTTPException(400, "Departamento inválido")
    if dept.id == t.department_id:
        return RedirectResponse(_url(t), status_code=302)
    old = t.department.name if t.department else ""
    t.department_id = dept.id
    t.assigned_to = None
    svc.apply_sla(t, db)
    db.commit()

    _notify(
        svc.department_admin_ids(dept.id, db) + [t.requester_id], user,
        f"{_actor(user)} transfirió un ticket a {dept.name}", t, "ticket_transfer",
    )
    realtime.notify_ticket(t.id)
    audit.log_action(
        "ticket_transfer", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=_label(t), details=f"{old} -> {dept.name}",
    )
    # Quien lo transfiere puede dejar de verlo (era de su departamento).
    return RedirectResponse(_url(t) if svc.can_view(user, t) else "/tickets/", status_code=302)


@router.post("/{number:int}/classify")
def classify_ticket(
    number: int,
    request: Request,
    priority: str = Form(...),
    kind: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_attend(user, t):
        raise HTTPException(403)
    if priority not in TASK_PRIORITIES or kind not in TICKET_KINDS:
        raise HTTPException(400)
    changes = []
    if priority != t.priority:
        changes.append(f"priority {t.priority} -> {priority}")
        t.priority = priority
        svc.apply_sla(t, db)
    if kind != t.kind:
        changes.append(f"kind {t.kind} -> {kind}")
        t.kind = kind
    db.commit()
    if changes:
        realtime.notify_ticket(t.id)
        audit.log_action(
            "ticket_classify", user=user, request=request,
            resource_type="ticket", resource_id=t.id, resource_name=_label(t), details="; ".join(changes),
        )
    return RedirectResponse(_url(t), status_code=302)


@router.post("/{number:int}/edit")
def edit_ticket(
    number: int,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_edit_text(user, t):
        raise HTTPException(403)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(400, "El título es obligatorio")
    t.title = title
    t.description = description.strip() or None
    db.commit()
    realtime.notify_ticket(t.id)
    audit.log_action(
        "ticket_edit", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=_label(t),
    )
    return RedirectResponse(_url(t), status_code=302)


@router.post("/{number:int}/delete")
def delete_ticket(
    number: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    if user.role != ROLE_SUPERADMIN:
        raise HTTPException(403)
    t = _get(number, db, user)
    label = _label(t)
    for ev in t.evidences:
        try:
            storage.delete_evidence(ev.file_key)
        except Exception:
            pass
    ticket_id = t.id
    db.delete(t)
    db.commit()
    realtime.notify_ticket(ticket_id)
    audit.log_action(
        "ticket_delete", user=user, request=request,
        resource_type="ticket", resource_id=ticket_id, resource_name=label,
    )
    return RedirectResponse("/tickets/", status_code=302)


# ── Evidencias ────────────────────────────────────────────────────────────────

@router.post("/{number:int}/evidences")
def upload_evidences(
    number: int,
    request: Request,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_comment(user, t):
        raise HTTPException(403)
    storage.check_upload_sizes(*files)
    names = _save_files(t, user, files, db)
    if names:
        svc.mark_responded(t, user)
    db.commit()
    if names:
        _notify(svc.followers(t, db), user, f"{_actor(user)} subió evidencia", t, "ticket_evidence")
        realtime.notify_ticket(t.id)
        audit.log_action(
            "ticket_evidence_upload", user=user, request=request,
            resource_type="ticket", resource_id=t.id, resource_name=_label(t),
            details=f"files={len(names)} names={','.join(names[:5])}",
        )
    return RedirectResponse(_url(t), status_code=302)


def _evidence(t: Ticket, evidence_id: str, db: Session) -> TicketEvidence:
    return attachments.get_or_404(db, TicketEvidence, TicketEvidence.ticket_id, t.id, evidence_id)


@router.post("/{number:int}/evidences/{evidence_id}/delete")
def delete_evidence(
    number: int,
    evidence_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    ev = _evidence(t, evidence_id, db)
    if str(ev.uploaded_by) != str(user.id) and not svc.can_manage(user, t):
        raise HTTPException(403)
    filename = attachments.remove(ev, db)
    db.commit()
    realtime.notify_ticket(t.id)
    audit.log_action(
        "ticket_evidence_delete", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=filename,
    )
    return RedirectResponse(_url(t), status_code=302)


@router.get("/{number:int}/evidences/{evidence_id}/download")
def download_evidence(
    number: int,
    evidence_id: str,
    inline: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    return attachments.download(_evidence(t, evidence_id, db), inline)


# ── Comentarios ───────────────────────────────────────────────────────────────

def _comment(t: Ticket, comment_id: str, db: Session) -> TicketComment:
    return comments.get_or_404(db, TicketComment, TicketComment.ticket_id, t.id, comment_id)


def _comment_ctx(t: Ticket, user: User, **extra) -> dict:
    return {
        "comment_base": _url(t),
        "current_user": user,
        "can_edit": svc.can_manage(user, t),
        "can_comment": svc.can_comment(user, t),
        "csrf_token": generate_csrf_token(str(user.id)),
        **extra,
    }


@router.post("/{number:int}/comments", response_class=HTMLResponse)
def add_comment(
    number: int,
    request: Request,
    content: str = Form(default=""),
    sticker: str = Form(default=""),
    parent_id: str = Form(default=""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_comment(user, t):
        raise HTTPException(403)
    parent = comments.thread_root(db, TicketComment, _comment(t, parent_id, db) if parent_id else None)
    content = comments.clean(content, sticker)
    comment = comments.create(db, TicketComment, user, content, parent, ticket_id=t.id)
    svc.mark_responded(t, user)
    t.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(comment)
    comment.user = user

    comments.notify(
        user, svc.followers(t, db), parent=parent, title=f"{_actor(user)} comentó en un ticket",
        body=_label(t), url=f"{_url(t)}#comment-{comment.id}", kind="ticket_comment",
    )
    realtime.notify_ticket(t.id)
    audit.log_action(
        "ticket_comment", user=user, request=request,
        resource_type="ticket", resource_id=t.id, resource_name=_label(t),
        details=("respuesta: " if parent else "") + ("sticker" if sticker else content[:80]),
    )
    return templates.TemplateResponse(
        request,
        "tasks/_comment.html" if parent else "tasks/_comment_thread.html",
        _comment_ctx(t, user, comment=comment, replies=[]),
    )


@router.post("/{number:int}/comments/{comment_id}/edit", response_class=HTMLResponse)
def edit_comment(
    number: int,
    comment_id: str,
    request: Request,
    content: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    comment = _comment(t, comment_id, db)
    comments.edit(comment, user, content)
    db.commit()
    db.refresh(comment)
    realtime.notify_ticket(t.id)
    return templates.TemplateResponse(request, "tasks/_comment.html", _comment_ctx(
        t, user, comment=comment,
        comment_reactions=reactions.summarize(db, TicketCommentReaction, [comment.id], user.id),
    ))


@router.post("/{number:int}/comments/{comment_id}/delete", response_class=HTMLResponse)
def delete_comment(
    number: int,
    comment_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    comment = _comment(t, comment_id, db)
    if str(comment.user_id) != str(user.id) and not svc.can_manage(user, t):
        raise HTTPException(403)
    db.delete(comment)
    db.commit()
    realtime.notify_ticket(t.id)
    return HTMLResponse("")


def _render_reactions(request: Request, t: Ticket, comment: TicketComment, user: User, db: Session):
    return comments.reactions_block(
        request, db, TicketCommentReaction, comment, user,
        url=f"{_url(t)}/comments/{comment.id}/reactions", readonly=not svc.can_comment(user, t),
    )


@router.get("/{number:int}/comments/{comment_id}/reactions", response_class=HTMLResponse)
def comment_reactions(
    number: int,
    comment_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    return _render_reactions(request, t, _comment(t, comment_id, db), user, db)


@router.post("/{number:int}/comments/{comment_id}/reactions", response_class=HTMLResponse)
def toggle_comment_reaction(
    number: int,
    comment_id: str,
    request: Request,
    emoji: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    user = _require_user(current_user)
    t = _get(number, db, user)
    if not svc.can_comment(user, t):
        raise HTTPException(403)
    comment = _comment(t, comment_id, db)
    reactions.toggle(db, TicketCommentReaction, comment.id, user.id, emoji)
    realtime.notify_reaction(realtime.ticket_channel(t.id), comment.id)
    return _render_reactions(request, t, comment, user, db)
