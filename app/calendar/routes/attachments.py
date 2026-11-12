"""Archivos / evidencias de un evento (mismos permisos que los comentarios)."""

import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app import audit, storage, images
from app.auth.deps import get_current_user
from app.calendar.reminders import to_local
from app.database import get_db
from app.models import CalendarEvent, CalendarEventAttachment, User
from app.messaging import realtime
from app.calendar.helpers import (
    _display_name,
    _visible_event,
)

router = APIRouter()


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


def _attachment_json(a: CalendarEventAttachment, user: User, ev: CalendarEvent) -> dict:
    return {
        "id": str(a.id),
        "filename": a.filename,
        "size": a.file_size,
        "is_image": a.content_type.startswith("image/"),
        "uploader": _display_name(a.uploader),
        "created_at": to_local(a.created_at).isoformat(timespec="minutes"),
        "url": f"/calendar/events/{ev.id}/attachments/{a.id}/download",
        # Borra quien lo subió o el dueño del evento.
        "can_delete": a.uploaded_by == user.id or ev.owner_id == user.id,
    }


def _event_attachment(ev: CalendarEvent, attachment_id: str, db: Session) -> CalendarEventAttachment:
    try:
        uuid.UUID(attachment_id)
    except ValueError:
        raise HTTPException(404)
    a = db.query(CalendarEventAttachment).filter(
        CalendarEventAttachment.id == attachment_id, CalendarEventAttachment.event_id == ev.id,
    ).first()
    if not a:
        raise HTTPException(404)
    return a


@router.get("/events/{event_id}/attachments")
def list_attachments(
    event_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    attachments = (
        db.query(CalendarEventAttachment)
        .options(joinedload(CalendarEventAttachment.uploader))
        .filter(CalendarEventAttachment.event_id == ev.id)
        .order_by(CalendarEventAttachment.created_at)
        .all()
    )
    return JSONResponse([_attachment_json(a, current_user, ev) for a in attachments])


@router.post("/events/{event_id}/attachments")
def upload_attachments(
    event_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
    date: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    files = [f for f in files if f.filename]
    if not files:
        raise HTTPException(422, "Elige al menos un archivo")
    storage.check_upload_sizes(*files)

    added = []
    for f in files:
        content, filename, content_type = images.optimize_image(f.file.read(), f.filename, f.content_type)
        key = f"calendar/{ev.id}/{uuid.uuid4()}_{_safe_filename(filename)}"
        content_type = (content_type or "application/octet-stream")[:100]
        storage.upload_evidence(key, content, content_type)
        a = CalendarEventAttachment(
            event_id=ev.id, uploaded_by=current_user.id, filename=filename[:255],
            file_key=key, content_type=content_type, file_size=len(content),
        )
        db.add(a)
        added.append(a)
    db.commit()

    # Avisa al dueño y a los compartidos (menos a quien sube), como los comentarios.
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        day = to_local(ev.start_at).date()
    actor = _display_name(current_user)
    for uid in ({ev.owner_id} | {sh.user_id for sh in ev.shares}) - {current_user.id}:
        realtime.notify_user(
            uid,
            title=f"{actor} adjuntó {'un archivo' if len(added) == 1 else f'{len(added)} archivos'} en «{ev.title}»",
            body=", ".join(a.filename for a in added)[:120],
            url=f"/calendar/?date={day.isoformat()}&event={ev.id}",
            kind="calendar_attachment",
        )
    audit.log_action(
        "calendar_attachment_upload", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
        details=f"files={len(added)} names={','.join(a.filename for a in added[:5])}",
    )
    return JSONResponse([_attachment_json(a, current_user, ev) for a in added])


@router.get("/events/{event_id}/attachments/{attachment_id}/download")
def download_attachment(
    event_id: str,
    attachment_id: str,
    inline: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    a = _event_attachment(ev, attachment_id, db)
    return RedirectResponse(storage.get_evidence_url(a.file_key, a.filename, inline=inline), status_code=302)


@router.post("/events/{event_id}/attachments/{attachment_id}/delete")
def delete_attachment(
    event_id: str,
    attachment_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    a = _event_attachment(ev, attachment_id, db)
    if a.uploaded_by != current_user.id and ev.owner_id != current_user.id:
        raise HTTPException(403)
    filename = a.filename
    try:
        storage.delete_evidence(a.file_key)
    except Exception:
        pass
    db.delete(a)
    db.commit()
    audit.log_action(
        "calendar_attachment_delete", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=filename,
    )
    return {"ok": True}
