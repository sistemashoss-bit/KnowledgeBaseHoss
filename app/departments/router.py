from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import audit
from app import valkey_client as vk
from app.auth import hoss
from app.auth.deps import require_role
from app.auth.provisioning import upsert_department
from app.auth.utils import generate_csrf_token
from app.database import get_db
from app.models import Branch, Department, ROLE_SUPERADMIN
from app.templating import templates

router = APIRouter(prefix="/departments", tags=["departments"])


@router.get("/", response_class=HTMLResponse)
def list_departments(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    depts = db.query(Department).order_by(Department.name).all()
    branches = db.query(Branch).order_by(Branch.name).all()
    csrf = generate_csrf_token(str(user.id))

    # Feedback del sync (viene por query param tras el redirect).
    sync_status = request.query_params.get("sync")
    sync_msg = None
    if sync_status == "ok":
        sync_msg = ("ok", f"Sincronización completada: {request.query_params.get('d', '0')} departamento(s) actualizados.")
    elif sync_status == "notoken":
        sync_msg = ("error", "No hay sesión de hoss activa. Vuelve a entrar por el acceso de hoss y reintenta.")
    elif sync_status == "error":
        sync_msg = ("error", "No se pudo sincronizar con hoss (sesión expirada, sin permiso view_departments o servicio no disponible).")

    return templates.TemplateResponse(
        request, "departments/list.html",
        {"departments": depts, "branches": branches, "current_user": user,
         "csrf_token": csrf, "sync_msg": sync_msg},
    )


@router.post("/sync")
async def sync_departments(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    token = vk.get_hoss_token(user.id)
    if not token:
        return RedirectResponse("/departments/?sync=notoken", status_code=302)

    departments = await hoss.fetch_departments(token)
    if departments is None:
        return RedirectResponse("/departments/?sync=error", status_code=302)

    count = 0
    for d in departments:
        gid = d.get("global_department_id")
        name = (d.get("name") or "").strip()
        if not gid or not name:
            continue
        upsert_department(db, gid, name)
        count += 1
    db.commit()

    audit.log_action(
        "departments_sync", user=user, request=request,
        resource_type="department", resource_name="departamentos",
        details=f"{count} departamento(s)",
    )
    return RedirectResponse(f"/departments/?sync=ok&d={count}", status_code=302)


@router.post("/{dept_id}/edit")
def edit_department(
    dept_id: str,
    name: str = Form(...),
    branch_id: str = Form(default=""),
    triage_description: str = Form(default=""),
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    dept = db.query(Department).filter(Department.id == dept_id).first()
    if not dept:
        raise HTTPException(404)
    dept.name = name.strip()
    dept.branch_id = branch_id or None
    dept.triage_description = triage_description.strip() or None
    db.commit()
    return RedirectResponse("/departments/", status_code=302)


@router.post("/{dept_id}/delete")
def delete_department(
    dept_id: str,
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    dept = db.query(Department).filter(Department.id == dept_id).first()
    if dept:
        db.delete(dept)
        db.commit()
    return RedirectResponse("/departments/", status_code=302)
