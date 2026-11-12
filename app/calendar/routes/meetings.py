"""Reuniones: confirmación de asistencia, token de videollamada y página de la reunión."""

import os
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload
from livekit.api import AccessToken, VideoGrants

from app.auth.deps import get_current_user
from app.auth.utils import generate_csrf_token
from app.calendar.reminders import iter_occurrences, to_local
from app.database import get_db
from app.models import (
    MEETING_JOIN_EARLY_MINUTES,
    MEETING_RESPONSES,
    CalendarEvent,
    CalendarEventShare,
    User,
)
from app.templating import templates
from app.calendar.helpers import (
    _display_name,
    _notify_meeting_owner,
    _repeat_label,
)

router = APIRouter()


def _meeting_for(event_id: str, user: User, db: Session) -> tuple[CalendarEvent, CalendarEventShare | None]:
    """Reunión de la que `user` es organizador o invitado (no basta con ver su
    calendario: la sala es solo para los convocados)."""
    try:
        uuid.UUID(event_id)
    except ValueError:
        raise HTTPException(404)
    ev = (
        db.query(CalendarEvent)
        .options(joinedload(CalendarEvent.owner), joinedload(CalendarEvent.shares).joinedload(CalendarEventShare.user))
        .filter(CalendarEvent.id == event_id, CalendarEvent.is_meeting.is_(True))
        .first()
    )
    if not ev:
        raise HTTPException(404)
    share = next((sh for sh in ev.shares if sh.user_id == user.id), None)
    if ev.owner_id != user.id and not share:
        raise HTTPException(403)
    return ev, share


def _meeting_occurrence(ev: CalendarEvent, now: datetime) -> tuple[datetime, datetime] | None:
    """(inicio, fin) UTC de la ocurrencia en curso o la próxima; None si ya no
    queda ninguna. En una serie, la de hoy mientras no termine."""
    duration = ev.end_at - ev.start_at
    for start in iter_occurrences(ev, to_local(now - duration).date()):
        if start + duration > now:
            return start, start + duration
    return None


def _join_window(occ: tuple[datetime, datetime] | None, now: datetime) -> str:
    """'open' (se puede entrar), 'early' (aún no) o 'ended'."""
    if not occ:
        return "ended"
    return "open" if now >= occ[0] - timedelta(minutes=MEETING_JOIN_EARLY_MINUTES) else "early"


@router.post("/events/{event_id}/rsvp")
def meeting_rsvp(
    event_id: str,
    response: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if response not in MEETING_RESPONSES:
        raise HTTPException(422, "Respuesta inválida")
    ev, share = _meeting_for(event_id, current_user, db)
    if not share:
        raise HTTPException(422, "El organizador no confirma asistencia")
    changed = share.response != response
    share.response, share.responded_at = response, datetime.utcnow()
    db.commit()
    if changed:
        what = {"yes": "asistirá a la reunión", "maybe": "tal vez asista a la reunión", "no": "no asistirá a la reunión"}
        _notify_meeting_owner(ev, current_user, what[response])
    return {"ok": True, "response": response}


@router.get("/events/{event_id}/meeting/token")
def meeting_token(
    event_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Token de LiveKit para la sala de la reunión, solo dentro de su horario
    (desde MEETING_JOIN_EARLY_MINUTES antes del inicio hasta que termina)."""
    if not current_user:
        raise HTTPException(401)
    ev, _ = _meeting_for(event_id, current_user, db)
    now = datetime.utcnow()
    window = _join_window(_meeting_occurrence(ev, now), now)
    if window != "open":
        raise HTTPException(
            403, "La reunión ya terminó" if window == "ended"
            else f"La sala abre {MEETING_JOIN_EARLY_MINUTES} minutos antes del inicio",
        )
    room = f"meet-{ev.id}"
    token = (
        AccessToken(os.getenv("LIVEKIT_API_KEY", ""), os.getenv("LIVEKIT_API_SECRET", ""))
        .with_identity(str(current_user.id))
        .with_name(_display_name(current_user))
        .with_grants(VideoGrants(room_join=True, room=room))
        .to_jwt()
    )
    return JSONResponse({"token": token, "url": os.getenv("LIVEKIT_URL", ""), "room": room})


@router.get("/meet/{event_id}", response_class=HTMLResponse)
def meeting_page(
    event_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    ev, share = _meeting_for(event_id, current_user, db)
    now = datetime.utcnow()
    occ = _meeting_occurrence(ev, now)
    start_local = to_local(occ[0]) if occ else to_local(ev.start_at)
    end_local = to_local(occ[1]) if occ else to_local(ev.end_at)
    counts = {k: 0 for k in MEETING_RESPONSES}
    for sh in ev.shares:
        if sh.response in counts:
            counts[sh.response] += 1
    return templates.TemplateResponse(
        request,
        "calendar/meeting.html",
        {
            "current_user": current_user,
            "ev": ev,
            "is_owner": ev.owner_id == current_user.id,
            "my_response": share.response if share else "",
            "invitees": [
                {"name": _display_name(sh.user), "response": sh.response or ""}
                for sh in sorted(ev.shares, key=lambda sh: _display_name(sh.user).lower())
            ],
            "counts": counts,
            "responses": MEETING_RESPONSES,
            "window": _join_window(occ, now),
            "join_early": MEETING_JOIN_EARLY_MINUTES,
            "start_local": start_local,
            "end_local": end_local,
            # Para que el navegador habilite "Unirse" a tiempo sin recargar.
            "opens_at_ms": int(((occ[0] if occ else ev.start_at) - timedelta(minutes=MEETING_JOIN_EARLY_MINUTES)
                                - datetime(1970, 1, 1)).total_seconds() * 1000),
            "repeat_label": _repeat_label(ev) if ev.frequency else "",
            "calendar_url": f"/calendar/?date={start_local.date().isoformat()}&event={ev.id}",
            "csrf_token": generate_csrf_token(str(current_user.id)),
        },
    )
