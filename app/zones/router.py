import re
import uuid
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user, require_superadmin
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
    return templates.TemplateResponse(
        request,
        "zones/list.html",
        {
            "current_user": current_user,
            "zones": zones,
            "branches_without_zone": branches_without_zone,
        },
    )


@router.post("/zones/", response_class=HTMLResponse)
def create_zone(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    slug = _unique_slug(db, Zone, _slugify(name))
    zone = Zone(id=uuid.uuid4(), name=name.strip(), slug=slug)
    db.add(zone)
    db.commit()
    return HTMLResponse(headers={"HX-Redirect": "/org/zones/"})


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

@router.post("/branches/", response_class=HTMLResponse)
def create_branch(
    request: Request,
    name: str = Form(...),
    zone_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    slug = _unique_slug(db, Branch, _slugify(name))
    branch = Branch(
        id=uuid.uuid4(),
        name=name.strip(),
        slug=slug,
        zone_id=zone_id if zone_id else None,
    )
    db.add(branch)
    db.commit()
    return HTMLResponse(headers={"HX-Redirect": "/org/zones/"})


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
