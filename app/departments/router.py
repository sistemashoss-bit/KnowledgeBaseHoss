import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app.models import Department, ROLE_SUPERADMIN
from app.templating import templates

router = APIRouter(prefix="/departments", tags=["departments"])


@router.get("/", response_class=HTMLResponse)
def list_departments(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    depts = db.query(Department).order_by(Department.name).all()
    csrf = generate_csrf_token(str(user.id))
    return templates.TemplateResponse(
        request, "departments/list.html",
        {"departments": depts, "current_user": user, "csrf_token": csrf},
    )


@router.post("/")
def create_department(
    name: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if db.query(Department).filter(Department.slug == slug).first():
        raise HTTPException(400, "Department already exists")
    db.add(Department(name=name, slug=slug))
    db.commit()
    return RedirectResponse("/departments/", status_code=302)


@router.post("/{dept_id}/delete")
def delete_department(
    dept_id: str,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    if not verify_csrf_token(csrf_token, str(user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    dept = db.query(Department).filter(Department.id == dept_id).first()
    if dept:
        db.delete(dept)
        db.commit()
    return RedirectResponse("/departments/", status_code=302)
