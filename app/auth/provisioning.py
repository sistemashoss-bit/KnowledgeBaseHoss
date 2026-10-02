"""JIT provisioning de usuarios a partir de una identidad verificada por hoss.

Compartido entre el login con credenciales (auth/router) y el SSO silencioso
por cookie de hoss (auth/deps), para tener una sola fuente de verdad.
"""
import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_SUPERADMIN, Department, User

_SITE_ROLES = {ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_EMPLOYEE}


def upsert_department(db: Session, gid, name: str) -> Department:
    """Departamento local para uno de hoss: por global_department_id, o enlaza por
    nombre uno local aún sin id global, o lo crea. hoss es el dueño del nombre."""
    dept = db.query(Department).filter(Department.global_department_id == gid).first()
    if not dept:
        dept = (
            db.query(Department)
            .filter(Department.global_department_id.is_(None), func.lower(Department.name) == name.lower())
            .first()
        )
        if dept:
            dept.global_department_id = gid
    if dept:
        dept.name = name
        return dept

    from app.zones.router import _slugify, _unique_slug

    dept = Department(
        id=uuid.uuid4(), global_department_id=gid, name=name,
        slug=_unique_slug(db, Department, _slugify(name)),
    )
    db.add(dept)
    db.flush()
    return dept


def _department_from_identity(identity: dict, db: Session):
    """Departamento local del puesto que manda hoss. None si hoss no manda
    departamento (puesto sin departamento todavía)."""
    hoss_dept = identity.get("department") or {}
    gid = hoss_dept.get("global_department_id")
    name = (hoss_dept.get("name") or "").strip()
    if not gid or not name:
        return None
    return upsert_department(db, gid, name)


def _sync_org(user: User, identity: dict, db: Session) -> bool:
    """hoss-api es dueño del puesto, del rol (derivado del puesto: site_role) y
    del departamento (el del puesto). Devuelve True si cambió algo.

    - site_role ausente (hoss sin esa versión) o desconocido: no se toca el rol.
    - Puesto sin departamento en hoss: se conserva el departamento local, para no
      dejar sin acceso a nadie mientras se clasifican los puestos.
    """
    changed = False
    # position ausente (hoss sin esa versión): no se toca; null: puesto sin asignar.
    if "position" in identity:
        position_name = (identity["position"] or {}).get("name")
        if user.position_name != position_name:
            user.position_name = position_name
            changed = True

    site_role = identity.get("site_role")
    if site_role in _SITE_ROLES and user.role != site_role:
        user.role = site_role
        changed = True

    dept = _department_from_identity(identity, db)
    if dept and user.department_id != dept.id:
        user.department_id = dept.id
        changed = True
    return changed


def provision_from_identity(identity: dict, db: Session) -> User:
    """Encuentra al usuario por corporate_id, o lo enlaza por email
    (pre-aprovisionamiento/backfill), o lo crea con rol genérico."""
    corporate_id = identity["corporate_id"]
    name = " ".join(
        p for p in (identity.get("first_name"), identity.get("last_name")) if p
    ) or None
    # hoss-api es dueño del avatar (SSO): manda la LLAVE de Wasabi en el identity.
    # knowledge solo la persiste y la refresca en cada login (es lo que muestra para
    # OTROS usuarios); la URL la firma localmente al renderizar.
    avatar_key = identity.get("avatar_key")

    user = db.query(User).filter(User.corporate_id == corporate_id).first()
    if user:
        changed = False
        if name and user.name != name:
            user.name = name
            changed = True
        if user.avatar_key != avatar_key:
            user.avatar_key = avatar_key
            changed = True
        if _sync_org(user, identity, db):
            changed = True
        if changed:
            db.commit()
        return user

    # Enlaza un usuario pre-aprovisionado por el admin (o preexistente) por email.
    # Rol y depto pasan a los de hoss (si hoss aún no los manda, se conservan).
    user = db.query(User).filter(User.email == identity["email"]).first()
    if user:
        user.corporate_id = corporate_id
        if name and not user.name:
            user.name = name
        user.avatar_key = avatar_key
        _sync_org(user, identity, db)
        db.commit()
        return user

    # Usuario nuevo: rol y depto de hoss; rol genérico si hoss no lo manda.
    user = User(
        id=uuid.uuid4(),
        corporate_id=corporate_id,
        email=identity["email"],
        name=name,
        avatar_key=avatar_key,
        role=ROLE_EMPLOYEE,
    )
    _sync_org(user, identity, db)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
