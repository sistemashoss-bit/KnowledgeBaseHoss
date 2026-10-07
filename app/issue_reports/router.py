"""Endpoint del widget de reportes (carpeta report-widget/, servido por CDN).

Otra app (hoss-front, meta…) embebe el widget; el usuario describe el problema,
adjunta un screenshot y aquí nace una tarea clasificada por Jev.

Autenticación, en este orden:
- `Authorization: Bearer <accessToken de hoss-api>` desde otras apps: se valida
  contra hoss-api (/sso/identity) y se aprovisiona al usuario como en el SSO.
  Requiere que el puesto tenga acceso a knowledge en hoss.
- Cookie de sesión de knowledge, cuando el widget corre dentro de knowledge
  (mismo origen; el CSRF global aplica como en cualquier POST).

CORS: solo para /api/issue-reports y solo para REPORT_WIDGET_ORIGINS, sin
credenciales (ver `ReportsCORSMiddleware` en main.py).
"""
import json
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import audit, storage
from app import valkey_client as vk
from app.auth import hoss
from app.auth.deps import get_current_user
from app.auth.provisioning import provision_from_identity
from app.config import settings
from app.database import get_db
from app.issue_reports.classify import classify
from app.messaging import realtime
from app.models import (
    Department, Task, TaskEvidence, TaskStatusHistory, User,
    PRIORITY_MEDIUM, ROLE_ADMIN, TASK_PENDING,
)

router = APIRouter(prefix="/api/issue-reports", tags=["issue-reports"])

MAX_DESCRIPTION = 4000
MAX_CONTEXT_BYTES = 20_000
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
MAX_ERRORS = 10
_SCREENSHOT_TYPES = {
    "image/webp": (b"RIFF", "webp"),
    "image/png": (b"\x89PNG", "png"),
    "image/jpeg": (b"\xff\xd8\xff", "jpg"),
}
# Por usuario: 10 reportes cada 10 minutos.
_RATE_LIMIT, _RATE_WINDOW = 10, 600


def _clip(value, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] or None


def _clean_context(raw: str) -> dict:
    """Solo las llaves conocidas, recortadas: el JSON viene del navegador."""
    if len(raw.encode()) > MAX_CONTEXT_BYTES:
        raise HTTPException(413, "Contexto demasiado grande")
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        raise HTTPException(400, "Contexto inválido")
    if not isinstance(data, dict):
        raise HTTPException(400, "Contexto inválido")

    errors = data.get("recent_errors")
    errors = errors[-MAX_ERRORS:] if isinstance(errors, list) else []
    ctx = {
        "app": _clip(data.get("app"), 50),
        "app_version": _clip(data.get("app_version"), 50),
        "url": _clip(data.get("url"), 1000),
        "page_title": _clip(data.get("page_title"), 300),
        "browser": _clip(data.get("browser"), 300),
        "viewport": _clip(data.get("viewport"), 30),
        "element": _clip(data.get("element"), 300),
        "recent_errors": [e for e in (_clip(x, 500) for x in errors) if e],
    }
    url = ctx["url"]
    if url and urlsplit(url).scheme not in ("http", "https"):
        ctx["url"] = None
    return {k: v for k, v in ctx.items() if v}


async def _reporter(request: Request, db: Session) -> User:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            identity = await hoss.introspect_session(auth[7:].strip())
        except hoss.AccessDenied:
            raise HTTPException(403, "Tu puesto no tiene acceso a knowledge en hoss")
        if not identity:
            raise HTTPException(401, "Sesión de hoss inválida o expirada")
        user = provision_from_identity(identity, db)
    else:
        user = get_current_user(request, db)
    if not user or not user.is_active:
        raise HTTPException(401, "Authentication required")
    return user


def _triage_department(db: Session) -> Department | None:
    return (
        db.query(Department)
        .filter(func.lower(Department.name) == settings.report_triage_department.lower())
        .first()
    )


def _title(description: str, app_name: str | None) -> str:
    first = description.strip().splitlines()[0].strip()
    if len(first) > 120:
        first = first[:117].rstrip() + "…"
    return f"[{app_name}] {first}" if app_name else first


@router.post("")
async def create_issue_report(
    request: Request,
    description: str = Form(...),
    context: str = Form("{}"),
    screenshot: UploadFile | None = File(default=None),
    db: Session = Depends(get_db),
):
    user = await _reporter(request, db)
    if vk.hit_rate_limit(f"issue_report:{user.id}", _RATE_LIMIT, _RATE_WINDOW):
        raise HTTPException(429, "Demasiados reportes; intenta en unos minutos")

    description = description.strip()
    if not description:
        raise HTTPException(400, "Describe el problema")
    description = description[:MAX_DESCRIPTION]
    ctx = _clean_context(context)

    shot = None
    if screenshot and screenshot.filename:
        content = await screenshot.read(MAX_SCREENSHOT_BYTES + 1)
        if len(content) > MAX_SCREENSHOT_BYTES:
            raise HTTPException(413, "La captura pesa más de 5 MB")
        kind = _SCREENSHOT_TYPES.get(screenshot.content_type or "")
        if not kind or not content.startswith(kind[0]):
            raise HTTPException(400, "La captura debe ser WebP, PNG o JPEG")
        shot = (content, screenshot.content_type, kind[1])

    # Clasificación. Si Jev falla, tarda o no está seguro: triage + prioridad media.
    departments = db.query(Department).order_by(Department.name).all()
    jev_state = {
        "app": ctx.get("app"),
        "page_title": ctx.get("page_title"),
        "page_path": urlsplit(ctx["url"]).path if ctx.get("url") else None,
        "description": description,
        "recent_errors": [e[:300] for e in ctx.get("recent_errors", [])[-5:]],
    }
    result = await classify({k: v for k, v in jev_state.items() if v}, departments)
    triage = _triage_department(db)
    department_id = (result or {}).get("department_id") or (triage.id if triage else None)
    priority = (result or {}).get("priority") or PRIORITY_MEDIUM
    if result:
        ctx["jev"] = result["jev"]

    task = Task(
        id=uuid.uuid4(),
        title=_title(description, ctx.get("app")),
        description=description,
        priority=priority,
        status=TASK_PENDING,
        department_id=department_id,
        created_by=user.id,
        source_app=ctx.get("app") or "widget",
        context=ctx,
    )
    db.add(task)
    db.flush()
    db.add(TaskStatusHistory(
        id=uuid.uuid4(), task_id=task.id, from_status=None, to_status=task.status,
        changed_by=user.id, changed_at=task.created_at,
    ))
    if shot:
        content, content_type, ext = shot
        key = f"tasks/{task.id}/{uuid.uuid4()}_captura.{ext}"
        storage.upload_evidence(key, content, content_type)
        db.add(TaskEvidence(
            id=uuid.uuid4(), task_id=task.id, uploaded_by=user.id,
            filename=f"captura.{ext}", file_key=key, content_type=content_type, file_size=len(content),
        ))
    db.commit()

    # Avisar a los admins del departamento destino: la tarea nace sin asignar.
    dept = db.get(Department, department_id) if department_id else None
    if dept:
        admin_ids = [
            uid for (uid,) in db.query(User.id).filter(
                User.department_id == dept.id, User.role == ROLE_ADMIN,
                User.is_active.is_(True), User.id != user.id,
            )
        ]
        actor = user.name or user.email
        for uid in admin_ids:
            realtime.notify_user(uid, title=f"Nuevo reporte de {actor}", body=task.title, url=f"/tasks/{task.id}")
    realtime.notify_tasks()
    audit.log_action(
        "issue_report", user=user, request=request,
        resource_type="task", resource_id=task.id, resource_name=task.title,
        details=f"app={task.source_app} priority={priority} classified={bool(result)}",
    )

    return {
        "id": str(task.id),
        "url": str(request.base_url).rstrip("/") + f"/tasks/{task.id}",
        "department": dept.name if dept else None,
        "priority": priority,
        "classified": bool(result),
    }
