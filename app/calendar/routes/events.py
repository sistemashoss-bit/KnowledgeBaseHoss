"""Eventos: crear, editar, mover, eliminar y dejar un evento compartido (solo el dueño, salvo dejar)."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session

from app import audit, storage
from app.auth.deps import get_current_user
from app.database import get_db
from app.models import CalendarEvent, CalendarEventShare, TASK_TAG_COLORS
from app.calendar.helpers import (
    _apply_recurrence,
    _apply_schedule,
    _display_name,
    _invite_title,
    _notify_meeting_owner,
    _notify_shared,
    _own_event,
    _parse_reminder,
    _refresh_reminder,
    _schedule_key,
    _set_shares,
    _visible_event,
)

router = APIRouter()


def _clean_color(color: str) -> str:
    return color if color in TASK_TAG_COLORS else "blue"


@router.post("/events")
def create_event(
    request: Request,
    title: str = Form(...),
    start: str = Form(...),
    end: str = Form(""),
    all_day: bool = Form(False),
    color: str = Form("blue"),
    location: str = Form(""),
    description: str = Form(""),
    reminder_minutes: str = Form(""),
    repeat: bool = Form(False),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    repeat_until: str = Form(""),
    shared_with: list[str] = Form([]),
    is_meeting: bool = Form(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")
    if is_meeting and all_day:
        raise HTTPException(422, "Una reunión necesita hora de inicio y fin")

    ev = CalendarEvent(
        is_meeting=is_meeting,
        owner_id=current_user.id,
        title=title,
        color=_clean_color(color),
        location=location.strip()[:255] or None,
        description=description.strip() or None,
        reminder_minutes=_parse_reminder(reminder_minutes),
    )
    _apply_schedule(ev, start, end, all_day)
    if _apply_recurrence(ev, repeat, frequency, day_of_week, day_of_month, days_of_week, repeat_until):
        _refresh_reminder(ev)
    added = _set_shares(ev, shared_with, db)
    db.add(ev)
    db.commit()
    _notify_shared(ev, added, current_user, _invite_title(ev, current_user))
    audit.log_action(
        "calendar_meeting_create" if ev.is_meeting else "calendar_event_create", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
    )
    return {"ok": True, "id": str(ev.id)}


@router.post("/events/{event_id}/edit")
def edit_event(
    event_id: str,
    request: Request,
    title: str = Form(...),
    start: str = Form(...),
    end: str = Form(""),
    all_day: bool = Form(False),
    color: str = Form("blue"),
    location: str = Form(""),
    description: str = Form(""),
    reminder_minutes: str = Form(""),
    repeat: bool = Form(False),
    frequency: str = Form("daily"),
    day_of_week: str = Form(""),
    day_of_month: str = Form(""),
    days_of_week: list[str] = Form([]),
    repeat_until: str = Form(""),
    shared_with: list[str] = Form([]),
    is_meeting: bool = Form(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _own_event(event_id, current_user, db)
    title = title.strip()[:255]
    if not title:
        raise HTTPException(422, "El título es obligatorio")
    if is_meeting and all_day:
        raise HTTPException(422, "Una reunión necesita hora de inicio y fin")

    was_meeting, before = ev.is_meeting, _schedule_key(ev)
    before_shares = {sh.user_id for sh in ev.shares}
    ev.is_meeting = is_meeting
    ev.title = title
    ev.color = _clean_color(color)
    ev.location = location.strip()[:255] or None
    ev.description = description.strip() or None
    new_reminder = _parse_reminder(reminder_minutes)
    reminder_changed = new_reminder != ev.reminder_minutes
    ev.reminder_minutes = new_reminder
    _apply_schedule(ev, start, end, all_day)
    recurrence_changed = _apply_recurrence(
        ev, repeat, frequency, day_of_week, day_of_month, days_of_week, repeat_until,
    )
    if reminder_changed or recurrence_changed:
        _refresh_reminder(ev)
    added = _set_shares(ev, shared_with, db)
    removed = before_shares - {sh.user_id for sh in ev.shares}
    kept = [sh.user_id for sh in ev.shares if sh.user_id not in added]
    rescheduled = ev.is_meeting and was_meeting and _schedule_key(ev) != before
    if rescheduled:
        for sh in ev.shares:
            sh.response, sh.responded_at = None, None
    db.commit()

    actor = _display_name(current_user)
    if ev.is_meeting and not was_meeting:
        # Un evento que se vuelve reunión: todos sus compartidos quedan invitados.
        _notify_shared(ev, list(added) + kept, current_user, _invite_title(ev, current_user))
    else:
        _notify_shared(ev, added, current_user, _invite_title(ev, current_user))
        if rescheduled:
            _notify_shared(ev, kept, current_user, f"{actor} cambió el horario de la reunión: confirma de nuevo")
    if ev.is_meeting and removed:
        _notify_shared(ev, removed, current_user, f"{actor} te quitó de la reunión")
    audit.log_action(
        "calendar_meeting_update" if ev.is_meeting else "calendar_event_update", user=current_user, request=request,
        resource_type="calendar_event", resource_id=ev.id, resource_name=ev.title,
    )
    return {"ok": True}


@router.post("/events/{event_id}/move")
def move_event(
    event_id: str,
    start: str = Form(...),
    end: str = Form(""),
    all_day: bool = Form(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Arrastrar o redimensionar en el calendario."""
    if not current_user:
        raise HTTPException(401)
    ev = _own_event(event_id, current_user, db)
    if ev.frequency:
        raise HTTPException(422, "Un evento que se repite se cambia desde Editar")
    if ev.is_meeting and all_day:
        raise HTTPException(422, "Una reunión necesita hora de inicio y fin")
    before = _schedule_key(ev)
    _apply_schedule(ev, start, end, all_day)
    rescheduled = ev.is_meeting and _schedule_key(ev) != before
    if rescheduled:
        for sh in ev.shares:
            sh.response, sh.responded_at = None, None
    db.commit()
    if rescheduled:
        _notify_shared(
            ev, [sh.user_id for sh in ev.shares], current_user,
            f"{_display_name(current_user)} cambió el horario de la reunión: confirma de nuevo",
        )
    return {"ok": True}


@router.post("/events/{event_id}/delete")
def delete_event(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    ev = _own_event(event_id, current_user, db)
    title = ev.title
    shared = [sh.user_id for sh in ev.shares]
    file_keys = [a.file_key for a in ev.attachments]
    _notify_shared(
        ev, shared, current_user,
        f"{_display_name(current_user)} canceló {'la reunión' if ev.is_meeting else 'un evento'}",
    )
    db.delete(ev)
    db.commit()
    for key in file_keys:
        try:
            storage.delete_evidence(key)
        except Exception:
            pass
    audit.log_action(
        "calendar_event_delete", user=current_user, request=request,
        resource_type="calendar_event", resource_id=event_id, resource_name=title,
    )
    return {"ok": True}


@router.post("/events/{event_id}/leave")
def leave_event(
    event_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Quien recibió un evento compartido lo quita de su calendario."""
    if not current_user:
        raise HTTPException(401)
    ev = _visible_event(event_id, current_user, db)
    deleted = db.query(CalendarEventShare).filter(
        CalendarEventShare.event_id == ev.id, CalendarEventShare.user_id == current_user.id,
    ).delete()
    db.commit()
    if deleted and ev.is_meeting:
        _notify_meeting_owner(ev, current_user, "salió de la reunión")
    return {"ok": True}
