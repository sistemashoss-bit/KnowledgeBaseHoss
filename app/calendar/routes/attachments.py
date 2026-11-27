"""Archivos / evidencias de un evento (mismos permisos que los comentarios)."""

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, joinedload

from app import attachments, audit, storage
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
    return attachments.get_or_404(db, CalendarEventAttachment, CalendarEventAttachment.event_id, ev.id, attachment_id)


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
    for f in attachments.store_uploads(f"calendar/{ev.id}", files):
        a = CalendarEventAttachment(event_id=ev.id, uploaded_by=current_user.id, **f)
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
    return attachments.download(_event_attachment(ev, attachment_id, db), inline)


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
    filename = attachments.remove(a, db)
    db.commit()
    audit.log_action(
        "calendar_attachment_delete", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=filename,
    )
    return {"ok": True}
