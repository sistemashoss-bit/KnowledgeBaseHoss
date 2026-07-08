import re
import uuid
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_superadmin
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app.models import Zone, Branch
from app.templating import templates

router = APIRouter(prefix="/org", tags=["org"])


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")


def _unique_slug(db: Session, model, base: str, exclude_id=None) -> str:
    slug = base
    n = 1
    while True:
        q = db.query(model).filter(model.slug == slug)
        if exclude_id:
            q = q.filter(model.id != exclude_id)
        if not q.first():
            return slug
        slug = f"{base}-{n}"
        n += 1


# ── Zones ──────────────────────────────────────────────────────────────────────

@router.get("/zones/", response_class=HTMLResponse)
def list_zones(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    zones = db.query(Zone).order_by(Zone.name).all()
    branches_without_zone = db.query(Branch).filter(Branch.zone_id.is_(None)).order_by(Branch.name).all()
    csrf = generate_csrf_token(str(current_user.id))
    return templates.TemplateResponse(
        request,
        "zones/list.html",
        {
            "current_user": current_user,
            "zones": zones,
            "branches_without_zone": branches_without_zone,
            "csrf_token": csrf,
        },
    )


@router.post("/zones/")
def create_zone(
    name: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    slug = _unique_slug(db, Zone, _slugify(name))
    db.add(Zone(id=uuid.uuid4(), name=name.strip(), slug=slug))
    db.commit()
    return RedirectResponse("/org/zones/", status_code=302)


@router.post("/zones/{zone_id}/edit")
def edit_zone(
    zone_id: str,
    name: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    zone = db.query(Zone).filter(Zone.id == zone_id).first()
    if not zone:
        raise HTTPException(404)
    zone.name = name.strip()
    zone.slug = _unique_slug(db, Zone, _slugify(name), exclude_id=zone_id)
    db.commit()
    return RedirectResponse("/org/zones/", status_code=302)


@router.delete("/zones/{zone_id}", response_class=HTMLResponse)
def delete_zone(
    zone_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    zone = db.query(Zone).filter(Zone.id == zone_id).first()
    if not zone:
        raise HTTPException(404)
    if zone.branches:
        raise HTTPException(400, "Mueve o elimina las sucursales antes de borrar la zona.")
    db.delete(zone)
    db.commit()
    return HTMLResponse(headers={"HX-Redirect": "/org/zones/"})


# ── Branches ───────────────────────────────────────────────────────────────────

@router.post("/branches/")
def create_branch(
    name: str = Form(...),
    zone_id: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    slug = _unique_slug(db, Branch, _slugify(name))
    db.add(Branch(
        id=uuid.uuid4(),
        name=name.strip(),
        slug=slug,
        zone_id=zone_id if zone_id else None,
    ))
    db.commit()
    return RedirectResponse("/org/zones/", status_code=302)


@router.post("/branches/{branch_id}/edit")
def edit_branch(
    branch_id: str,
    name: str = Form(...),
    zone_id: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(404)
    branch.name = name.strip()
    branch.slug = _unique_slug(db, Branch, _slugify(name), exclude_id=branch_id)
    branch.zone_id = zone_id if zone_id else None
    db.commit()
    return RedirectResponse("/org/zones/", status_code=302)


@router.delete("/branches/{branch_id}", response_class=HTMLResponse)
def delete_branch(
    branch_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(404)
    if branch.users or branch.departments:
        raise HTTPException(400, "Reasigna usuarios y departamentos antes de eliminar la sucursal.")
    db.delete(branch)
    db.commit()
    return HTMLResponse(headers={"HX-Redirect": "/org/zones/"})
