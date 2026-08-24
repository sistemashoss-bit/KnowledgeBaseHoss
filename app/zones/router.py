import re
import uuid
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import require_superadmin
from app.auth.utils import generate_csrf_token, verify_csrf_token
from app.database import get_db
from app import audit
from app.auth import hoss
from app import valkey_client as vk
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

    # Feedback del sync (viene por query param tras el redirect).
    sync_status = request.query_params.get("sync")
    sync_msg = None
    if sync_status == "ok":
        z = request.query_params.get("z", "0")
        b = request.query_params.get("b", "0")
        sync_msg = ("ok", f"Sincronización completada: {z} zona(s) y {b} sucursal(es) actualizadas.")
    elif sync_status == "notoken":
        sync_msg = ("error", "No hay sesión de hoss activa. Vuelve a entrar por el acceso de hoss y reintenta.")
    elif sync_status == "error":
        sync_msg = ("error", "No se pudo sincronizar con hoss (sesión expirada o servicio no disponible).")

    return templates.TemplateResponse(
        request,
        "zones/list.html",
        {
            "current_user": current_user,
            "zones": zones,
            "branches_without_zone": branches_without_zone,
            "csrf_token": csrf,
            "sync_msg": sync_msg,
        },
    )


# ── Sincronización desde hoss (hoss es el dueño del catálogo) ────────────────────

def _apply_org_sync(db: Session, data: dict) -> dict:
    """Upsert de regiones→zonas y sucursales→sucursales, mapeando por el UUID
    global de hoss (global_region_id / global_branch_id). Fallback por nombre para no duplicar en el
    primer sync. No borra nada local (evita cascadas sobre usuarios/departamentos)."""
    regions = data.get("regions", [])
    branches = data.get("branches", [])

    # hoss region id (int) -> global_region_id (uuid), para ligar sucursales.
    region_global_by_hossid: dict = {}
    zones_count = 0

    for reg in regions:
        gid = reg.get("global_region_id")
        name = (reg.get("name") or "").strip()
        if not gid or not name:
            continue
        region_global_by_hossid[reg.get("id")] = gid

        zone = db.query(Zone).filter(Zone.global_region_id == gid).first()
        if not zone:
            # Primer sync: adopta una zona local existente con el mismo nombre.
            zone = (
                db.query(Zone)
                .filter(Zone.global_region_id.is_(None), func.lower(Zone.name) == name.lower())
                .first()
            )
        if zone:
            zone.global_region_id = gid
            zone.name = name
        else:
            db.add(Zone(id=uuid.uuid4(), global_region_id=gid, name=name,
                        slug=_unique_slug(db, Zone, _slugify(name))))
        zones_count += 1

    db.flush()

    branches_count = 0
    for br in branches:
        gid = br.get("global_branch_id")
        name = (br.get("name") or "").strip()
        if not gid or not name:
            continue

        # Zona destino a partir de la región de hoss.
        zone_id = None
        reg_gid = region_global_by_hossid.get(br.get("region_id"))
        if reg_gid:
            z = db.query(Zone).filter(Zone.global_region_id == reg_gid).first()
            zone_id = z.id if z else None

        branch = db.query(Branch).filter(Branch.global_branch_id == gid).first()
        if not branch:
            branch = (
                db.query(Branch)
                .filter(Branch.global_branch_id.is_(None), func.lower(Branch.name) == name.lower())
                .first()
            )
        if branch:
            branch.global_branch_id = gid
            branch.name = name
            branch.zone_id = zone_id
        else:
            db.add(Branch(id=uuid.uuid4(), global_branch_id=gid, name=name,
                          slug=_unique_slug(db, Branch, _slugify(name)), zone_id=zone_id))
        branches_count += 1

    db.commit()
    return {"zones": zones_count, "branches": branches_count}


@router.post("/zones/sync")
async def sync_org(
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")

    token = vk.get_hoss_token(current_user.id)
    if not token:
        return RedirectResponse("/org/zones/?sync=notoken", status_code=302)

    data = await hoss.fetch_org(token)
    if data is None:
        return RedirectResponse("/org/zones/?sync=error", status_code=302)

    result = _apply_org_sync(db, data)
    audit.log_action(
        "org_sync", user=current_user, request=request,
        resource_type="org", resource_name="sucursales/regiones",
        details=f"{result['zones']} zona(s), {result['branches']} sucursal(es)",
    )
    return RedirectResponse(
        f"/org/zones/?sync=ok&z={result['zones']}&b={result['branches']}",
        status_code=302,
    )


@router.post("/zones/")
def create_zone(
    request: Request,
    name: str = Form(...),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    slug = _unique_slug(db, Zone, _slugify(name))
    zone = Zone(id=uuid.uuid4(), name=name.strip(), slug=slug)
    db.add(zone)
    db.commit()
    audit.log_action(
        "zone_create", user=current_user, request=request,
        resource_type="zone", resource_id=zone.id, resource_name=zone.name,
    )
    return RedirectResponse("/org/zones/", status_code=302)


@router.post("/zones/{zone_id}/edit")
def edit_zone(
    zone_id: str,
    request: Request,
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
    old_name = zone.name
    zone.name = name.strip()
    zone.slug = _unique_slug(db, Zone, _slugify(name), exclude_id=zone_id)
    db.commit()
    audit.log_action(
        "zone_edit", user=current_user, request=request,
        resource_type="zone", resource_id=zone_id, resource_name=zone.name,
        details=f"{old_name!r} → {zone.name!r}",
    )
    return RedirectResponse("/org/zones/", status_code=302)


@router.delete("/zones/{zone_id}", response_class=HTMLResponse)
def delete_zone(
    zone_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    zone = db.query(Zone).filter(Zone.id == zone_id).first()
    if not zone:
        raise HTTPException(404)
    if zone.branches:
        raise HTTPException(400, "Mueve o elimina las sucursales antes de borrar la zona.")
    name = zone.name
    db.delete(zone)
    db.commit()
    audit.log_action(
        "zone_delete", user=current_user, request=request,
        resource_type="zone", resource_id=zone_id, resource_name=name,
    )
    return HTMLResponse(headers={"HX-Redirect": "/org/zones/"})


# ── Branches ───────────────────────────────────────────────────────────────────

@router.post("/branches/")
def create_branch(
    request: Request,
    name: str = Form(...),
    zone_id: str = Form(""),
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    if not verify_csrf_token(csrf_token, str(current_user.id)):
        raise HTTPException(403, "Invalid CSRF token")
    slug = _unique_slug(db, Branch, _slugify(name))
    branch = Branch(id=uuid.uuid4(), name=name.strip(), slug=slug, zone_id=zone_id if zone_id else None)
    db.add(branch)
    db.commit()
    audit.log_action(
        "branch_create", user=current_user, request=request,
        resource_type="branch", resource_id=branch.id, resource_name=branch.name,
    )
    return RedirectResponse("/org/zones/", status_code=302)


@router.post("/branches/{branch_id}/edit")
def edit_branch(
    branch_id: str,
    request: Request,
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
    old_name = branch.name
    branch.name = name.strip()
    branch.slug = _unique_slug(db, Branch, _slugify(name), exclude_id=branch_id)
    branch.zone_id = zone_id if zone_id else None
    db.commit()
    audit.log_action(
        "branch_edit", user=current_user, request=request,
        resource_type="branch", resource_id=branch_id, resource_name=branch.name,
        details=f"{old_name!r} → {branch.name!r}",
    )
    return RedirectResponse("/org/zones/", status_code=302)


@router.delete("/branches/{branch_id}", response_class=HTMLResponse)
def delete_branch(
    branch_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(require_superadmin),
):
    branch = db.query(Branch).filter(Branch.id == branch_id).first()
    if not branch:
        raise HTTPException(404)
    if branch.users or branch.departments:
        raise HTTPException(400, "Reasigna usuarios y departamentos antes de eliminar la sucursal.")
    name = branch.name
    db.delete(branch)
    db.commit()
    audit.log_action(
        "branch_delete", user=current_user, request=request,
        resource_type="branch", resource_id=branch_id, resource_name=name,
    )
    return HTMLResponse(headers={"HX-Redirect": "/org/zones/"})
