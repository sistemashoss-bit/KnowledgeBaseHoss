"""Calendarios agregados (suscripciones): agregar, mostrar/ocultar y quitar."""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import audit
from app.auth.deps import get_current_user
from app.database import get_db
from app.models import CalendarSubscription, Department, User, CALENDAR_MAX_SUBSCRIPTIONS
from app.permissions import calendar_department_ids, calendar_members_query
from app.calendar.helpers import (
    _can_subscribe,
    _display_name,
)

router = APIRouter()


@router.post("/subscriptions")
def add_subscription(
    request: Request,
    target_user_id: str = Form(""),
    target_department_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    if not _can_subscribe(current_user):
        raise HTTPException(403)
    if bool(target_user_id) == bool(target_department_id):
        raise HTTPException(422, "Elige una persona o un departamento")

    count = db.query(CalendarSubscription).filter(CalendarSubscription.viewer_id == current_user.id).count()
    if count >= CALENDAR_MAX_SUBSCRIPTIONS:
        raise HTTPException(422, f"Máximo {CALENDAR_MAX_SUBSCRIPTIONS} calendarios agregados")

    try:
        target_id = uuid.UUID(target_user_id or target_department_id)
    except ValueError:
        raise HTTPException(404)

    if target_user_id:
        target = calendar_members_query(current_user, db).filter(User.id == target_id).first()
        if not target:
            raise HTTPException(403)
        exists = CalendarSubscription.target_user_id == target_id
        sub = CalendarSubscription(viewer_id=current_user.id, target_user_id=target_id)
        name = _display_name(target)
    else:
        allowed = calendar_department_ids(current_user, db)
        dept = db.query(Department).filter(Department.id == target_id).first()
        if not dept or (allowed is not None and target_id not in allowed):
            raise HTTPException(403)
        exists = CalendarSubscription.target_department_id == target_id
        sub = CalendarSubscription(viewer_id=current_user.id, target_department_id=target_id)
        name = dept.name

    if not db.query(CalendarSubscription).filter(CalendarSubscription.viewer_id == current_user.id, exists).first():
        db.add(sub)
        db.commit()
        audit.log_action(
            "calendar_subscription_add", user=current_user, request=request,
            resource_type="calendar_subscription", resource_id=sub.id, resource_name=name,
        )
    return RedirectResponse("/calendar/", status_code=302)


def _own_subscription(sub_id: str, user: User, db: Session) -> CalendarSubscription:
    try:
        uuid.UUID(sub_id)
    except ValueError:
        raise HTTPException(404)
    sub = db.query(CalendarSubscription).filter(
        CalendarSubscription.id == sub_id, CalendarSubscription.viewer_id == user.id,
    ).first()
    if not sub:
        raise HTTPException(404)
    return sub


@router.post("/subscriptions/{sub_id}/toggle")
def toggle_subscription(
    sub_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    sub = _own_subscription(sub_id, current_user, db)
    sub.is_visible = not sub.is_visible
    db.commit()
    return {"ok": True, "is_visible": sub.is_visible}


@router.post("/subscriptions/{sub_id}/delete")
def delete_subscription(
    sub_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        raise HTTPException(401)
    sub = _own_subscription(sub_id, current_user, db)
    db.delete(sub)
    db.commit()
    audit.log_action(
        "calendar_subscription_remove", user=current_user, request=request,
        resource_type="calendar_subscription", resource_id=sub_id,
    )
    return RedirectResponse("/calendar/", status_code=302)
