"""Capacitación: videos subidos a Mux con el mismo alcance que los documentos.

- Ver: permissions.can_access_training (público sin login, empleados, personas
  específicas + admin/accesos del departamento + quien lo subió).
- Subir: admin (solo su departamento) y superadmin, como /documents/upload.
- Gestionar (editar, eliminar): permissions.can_manage_training.

El archivo va del navegador directo a Mux (direct upload, ver mux.py); el
estado del video llega por webhook (/trainings/mux/webhook) o se consulta a
la API al abrirlo.
"""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app import audit
from app.auth.deps import get_current_user, require_auth, require_role
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app.models import (
    Department, Training, TrainingAllowedUser, User,
    ROLE_ADMIN, ROLE_SUPERADMIN, STATUSES, STATUS_CUSTOM,
    TRAINING_ERRORED, TRAINING_PROCESSING, TRAINING_READY, TRAINING_UPLOADING,
)
from app.permissions import can_access_training, can_manage_training, visible_trainings_filter
from app.templating import templates
from app.trainings import mux

log = logging.getLogger(__name__)

router = APIRouter(prefix="/trainings", tags=["trainings"])


def _training(training_id: str, db: Session) -> Training:
    try:
        uuid.UUID(training_id)
    except ValueError:
        raise HTTPException(404)
    t = (
        db.query(Training)
        .options(joinedload(Training.department), joinedload(Training.uploaded_by_user),
                 joinedload(Training.allowed_users).joinedload(TrainingAllowedUser.user))
        .filter(Training.id == training_id)
        .first()
    )
    if not t:
        raise HTTPException(404)
    return t


def _visible(training_id: str, user, db: Session) -> Training:
    t = _training(training_id, db)
    if not can_access_training(user, t):
        raise HTTPException(403 if user else 401)
    return t


def _managed(training_id: str, user, db: Session) -> Training:
    t = _training(training_id, db)
    if not can_manage_training(user, t):
        raise HTTPException(403, "No puedes gestionar esta capacitación")
    return t


def _refresh(t: Training, db: Session) -> None:
    """Si el video, su transcripción o sus capítulos no están listos, pregunta
    a Mux (por si no llegó el webhook)."""
    try:
        changed = mux.refresh(t)
        changed = mux.sync_extras(t) or changed
    except mux.MuxError as e:
        log.warning("No se pudo consultar Mux para %s: %s", t.id, e)
        changed = True  # lo que sí avanzó antes del error también se guarda
    except Exception:
        log.exception("Error al sincronizar transcripción/capítulos de %s", t.id)
        changed = False
    if changed:
        db.commit()


def _members(user: User, department_id, db: Session, *, any_department: bool) -> list[User]:
    q = db.query(User).options(joinedload(User.department)).filter(User.is_active.is_(True))
    if not any_department and user.role != ROLE_SUPERADMIN:
        q = q.filter(User.department_id == department_id)
    return q.order_by(User.name, User.email).all()


def _allowed_ids(status: str, ids: list[str], db: Session, department_id=None) -> list:
    """Personas válidas para 'personas específicas' (activas; al crear, del departamento)."""
    if status != STATUS_CUSTOM:
        return []
    try:
        wanted = {uuid.UUID(i) for i in ids if i}
    except ValueError:
        raise HTTPException(422, "Persona inválida")
    if not wanted:
        raise HTTPException(422, "Selecciona al menos una persona para visibilidad 'personas específicas'.")
    q = db.query(User.id).filter(User.is_active.is_(True), User.id.in_(wanted))
    if department_id is not None:
        q = q.filter(User.department_id == department_id)
    valid = {row[0] for row in q.all()}
    if valid != wanted:
        raise HTTPException(422, "Alguna de las personas seleccionadas no existe, está inactiva o no pertenece al departamento.")
    return sorted(valid)


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── Lista ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def list_trainings(
    request: Request,
    q: str = "",
    department_id: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    query = db.query(Training).options(joinedload(Training.department), joinedload(Training.allowed_users))
    cond = visible_trainings_filter(user)
    if cond is not None:
        query = query.filter(cond)
    q = q.strip()
    if q:
        like = f"%{q}%"
        query = query.filter(Training.title.ilike(like) | Training.description.ilike(like) | Training.transcript.ilike(like))
    if department_id:
        try:
            query = query.filter(Training.department_id == uuid.UUID(department_id))
        except ValueError:
            department_id = ""
    trainings = list(dict.fromkeys(query.order_by(Training.created_at.desc()).all()))

    cards = []
    for t in trainings:
        manage = can_manage_training(user, t)
        if t.video_status != TRAINING_READY and not manage:
            continue  # quien solo ve no necesita los que aún no se pueden reproducir
        try:
            thumb = mux.thumbnail_url(t) if t.video_status == TRAINING_READY else None
        except Exception:  # llave de firma mal configurada: la tarjeta sale sin miniatura
            log.exception("No se pudo firmar la miniatura de %s", t.id)
            thumb = None
        cards.append({"t": t, "thumb": thumb, "duration": _fmt_duration(t.duration), "can_manage": manage})

    return templates.TemplateResponse(
        request, "trainings/list.html",
        {
            "current_user": user,
            "cards": cards,
            "query": q,
            "selected_dept": department_id,
            "departments": db.query(Department).order_by(Department.name).all(),
            "can_upload": bool(user) and user.role in (ROLE_ADMIN, ROLE_SUPERADMIN),
            "mux_enabled": mux.enabled(),
            "csrf_token": generate_csrf_token(str(user.id)) if user else "",
        },
    )


# ── Subir ─────────────────────────────────────────────────────────────────────

@router.get("/new", response_class=HTMLResponse)
def new_training_form(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    if user.role == ROLE_SUPERADMIN:
        departments = db.query(Department).order_by(Department.name).all()
    else:
        departments = [user.department] if user.department else []
    return templates.TemplateResponse(
        request, "trainings/new.html",
        {
            "current_user": user,
            "departments": departments,
            "statuses": STATUSES,
            "members": _members(user, user.department_id, db, any_department=False),
            "mux_enabled": mux.enabled(),
            "csrf_token": generate_csrf_token(str(user.id)),
        },
    )


@router.post("/")
def create_training(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    department_id: str = Form(""),
    status: str = Form(...),
    allowed_user_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN, ROLE_ADMIN)),
):
    """Crea la capacitación y el direct upload de Mux. El navegador sube el
    archivo a `upload_url` y luego avisa a /trainings/{id}/uploaded."""
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")
    if status not in STATUSES:
        raise HTTPException(422, "Visibilidad inválida")
    if user.role == ROLE_ADMIN:
        department_id = str(user.department_id) if user.department_id else ""  # admin: solo el suyo
    try:
        dept = db.query(Department).filter(Department.id == uuid.UUID(department_id)).first()
    except ValueError:
        dept = None
    if not dept:
        raise HTTPException(422, "Elige un departamento")
    allowed = _allowed_ids(status, allowed_user_ids, db, department_id=dept.id)

    t = Training(
        id=uuid.uuid4(), title=title, description=description.strip() or None, department_id=dept.id,
        status=status, uploaded_by=user.id, video_status=TRAINING_UPLOADING,
        captions_status=mux.PENDING,  # se piden en el direct upload (mux.create_upload)
    )
    try:
        t.mux_upload_id, upload_url, t.playback_policy = mux.create_upload(str(t.id))
    except mux.MuxError as e:
        raise HTTPException(502, str(e))
    t.allowed_users = [TrainingAllowedUser(user_id=uid) for uid in allowed]
    db.add(t)
    db.commit()
    audit.log_action(
        "training_create", user=user, request=request,
        resource_type="training", resource_id=t.id, resource_name=t.title,
    )
    return {"id": str(t.id), "upload_url": upload_url}


@router.post("/{training_id}/uploaded")
def training_uploaded(training_id: str, db: Session = Depends(get_db), user=Depends(require_auth)):
    """El navegador terminó de subir el archivo a Mux."""
    t = _managed(training_id, user, db)
    if t.video_status == TRAINING_UPLOADING:
        t.video_status = TRAINING_PROCESSING
        db.commit()
    _refresh(t, db)
    return {"ok": True, "url": f"/trainings/{t.id}"}


# ── Ver ───────────────────────────────────────────────────────────────────────

@router.get("/{training_id}", response_class=HTMLResponse)
def view_training(
    training_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    t = _visible(training_id, user, db)
    _refresh(t, db)
    manage = can_manage_training(user, t)
    if t.video_status != TRAINING_READY and not manage:
        raise HTTPException(404)
    tokens = None
    if t.video_status == TRAINING_READY and t.playback_policy == "signed":
        try:
            tokens = mux.playback_tokens(t.mux_playback_id)
        except Exception:
            log.exception("No se pudo firmar la reproducción de %s", t.id)
            raise HTTPException(500, "La reproducción firmada no está bien configurada (MUX_SIGNING_KEY_*)")
    audit.log_action(
        "training_view", user=user, request=request,
        resource_type="training", resource_id=t.id, resource_name=t.title,
    )
    return templates.TemplateResponse(
        request, "trainings/view.html",
        {
            "current_user": user,
            "t": t,
            "tokens": tokens,
            "duration": _fmt_duration(t.duration),
            "chapters": [{**c, "label": _fmt_duration(int(c["start_time"])) or "0:00"} for c in (t.chapters or [])],
            "can_manage": manage,
            "csrf_token": generate_csrf_token(str(user.id)) if user else "",
        },
    )


@router.get("/{training_id}/status")
def training_status(training_id: str, db: Session = Depends(get_db), user=Depends(require_auth)):
    t = _managed(training_id, user, db)
    _refresh(t, db)
    return {"status": t.video_status}


# ── Editar / eliminar ─────────────────────────────────────────────────────────

@router.get("/{training_id}/edit", response_class=HTMLResponse)
def edit_training_form(
    training_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    t = _managed(training_id, user, db)
    return templates.TemplateResponse(
        request, "trainings/edit.html",
        {
            "current_user": user,
            "t": t,
            "statuses": STATUSES,
            # Editar = también compartir: cualquier persona activa, como en documentos.
            "members": _members(user, t.department_id, db, any_department=True),
            "selected_ids": {str(au.user_id) for au in t.allowed_users},
            "csrf_token": generate_csrf_token(str(user.id)),
        },
    )


@router.post("/{training_id}/edit")
def edit_training(
    training_id: str,
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    status: str = Form(...),
    allowed_user_ids: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    t = _managed(training_id, user, db)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")
    if status not in STATUSES:
        raise HTTPException(422, "Visibilidad inválida")
    allowed = _allowed_ids(status, allowed_user_ids, db)
    t.title, t.description, t.status = title, description.strip() or None, status
    current = {au.user_id: au for au in t.allowed_users}
    t.allowed_users = [current.get(uid) or TrainingAllowedUser(user_id=uid) for uid in allowed]
    db.commit()
    audit.log_action(
        "training_edit", user=user, request=request,
        resource_type="training", resource_id=t.id, resource_name=t.title,
        details=f"status={status} users={len(allowed)}",
    )
    return RedirectResponse(f"/trainings/{t.id}", status_code=302)


@router.post("/{training_id}/delete")
def delete_training(
    training_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_auth),
):
    t = _managed(training_id, user, db)
    try:
        if t.mux_asset_id:
            mux.delete_asset(t.mux_asset_id)
        elif t.mux_upload_id:
            mux.cancel_upload(t.mux_upload_id)
    except mux.MuxError as e:
        raise HTTPException(502, f"No se pudo borrar el video en Mux: {e}")
    title = t.title
    db.delete(t)
    db.commit()
    audit.log_action(
        "training_delete", user=user, request=request,
        resource_type="training", resource_id=training_id, resource_name=title,
    )
    return RedirectResponse("/trainings/", status_code=302)


# ── Webhook de Mux ────────────────────────────────────────────────────────────

@router.post("/mux/webhook")
async def mux_webhook(request: Request, db: Session = Depends(get_db)):
    """Eventos de Mux (configurar en el dashboard: <public_url>/trainings/mux/webhook)."""
    body = await request.body()
    if not mux.verify_webhook(body, request.headers.get("mux-signature", "")):
        raise HTTPException(401, "Firma inválida")
    event = json.loads(body)
    kind, data = event.get("type", ""), event.get("data") or {}

    t = None
    if kind.startswith("robots.v1.job.generate_chapters."):
        t = db.query(Training).filter(Training.chapters_job_id == data.get("id")).first()
    elif kind.startswith("video.asset.track."):
        t = db.query(Training).filter(Training.mux_asset_id == data.get("asset_id")).first()
    elif kind.startswith("video.upload."):
        t = db.query(Training).filter(Training.mux_upload_id == data.get("id")).first()
    elif kind.startswith("video.asset."):
        t = db.query(Training).filter(Training.mux_asset_id == data.get("id")).first()
        if not t and data.get("passthrough"):
            try:
                t = db.query(Training).filter(Training.id == uuid.UUID(data["passthrough"])).first()
            except ValueError:
                t = None
    if not t:
        return JSONResponse({"ok": True, "ignored": True})

    if kind == "video.upload.asset_created":
        t.mux_asset_id = data.get("asset_id") or t.mux_asset_id
        if t.video_status == TRAINING_UPLOADING:
            t.video_status = TRAINING_PROCESSING
    elif kind in ("video.upload.errored", "video.upload.cancelled"):
        t.video_status = TRAINING_ERRORED
    elif kind in ("video.asset.created", "video.asset.ready", "video.asset.errored", "video.asset.updated"):
        for key, value in mux.asset_fields(data).items():
            setattr(t, key, value)
    elif kind == "video.asset.deleted":
        t.video_status = TRAINING_ERRORED
    db.commit()
    if kind in ("video.asset.ready",) or kind.startswith(("video.asset.track.", "robots.v1.job.generate_chapters.")):
        # Subtítulos listos → transcripción y capítulos; trabajo de capítulos
        # terminado → guardarlos. Se consulta la API en vez de confiar en el payload.
        _refresh(t, db)
    return {"ok": True}
