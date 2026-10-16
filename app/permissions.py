from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Document, Folder, User


def granted_department_ids(user: "User", level: str | None = None) -> set:
    """Departamentos con acceso extra (UserDepartmentAccess). Sin `level`, los
    de cualquier nivel (manage incluye ver)."""
    return {a.department_id for a in user.department_access if level is None or a.level == level}


def overseen_department_ids(user: "User") -> set:
    """Departamentos completos que ve más allá de lo suyo como persona: el propio
    si es admin (sin importar sus sucursales) más los accesos extra."""
    from app.models import ROLE_ADMIN

    ids = granted_department_ids(user)
    if user.role == ROLE_ADMIN and user.department_id:
        ids.add(user.department_id)
    return ids


def managed_department_ids(user: "User") -> set:
    """Departamentos cuyos documentos y proyectos gestiona: el propio si es admin
    más los accesos `manage`. Documentos y proyectos no se acotan por sucursal."""
    from app.models import ACCESS_MANAGE, ROLE_ADMIN

    ids = granted_department_ids(user, ACCESS_MANAGE)
    if user.role == ROLE_ADMIN and user.department_id:
        ids.add(user.department_id)
    return ids


def managed_branch_ids(user: "User") -> set | None:
    """Sucursales a las que está acotado un admin en su propio departamento, o
    None si lo gestiona completo (departamento sin `branch_scoped`, o su puesto
    cubre todas las sucursales)."""
    from app.models import ROLE_ADMIN

    if user.role != ROLE_ADMIN or not user.department or not user.department.branch_scoped or user.all_branches:
        return None
    return {ub.branch_id for ub in user.user_branches}


def is_manager(user: "User") -> bool:
    """Encargado: admin/superadmin, o alguien con acceso `manage` a algún
    departamento (gestiona su trabajo como su admin)."""
    from app.models import ACCESS_MANAGE, ROLE_ADMIN, ROLE_SUPERADMIN

    return user.role in (ROLE_ADMIN, ROLE_SUPERADMIN) or bool(granted_department_ids(user, ACCESS_MANAGE))


def has_oversight(user: "User") -> bool:
    """Ve el trabajo de otros (pestaña Departamento, calendarios, reportes):
    admin/superadmin o cualquier acceso extra."""
    from app.models import ROLE_ADMIN, ROLE_SUPERADMIN

    return user.role in (ROLE_ADMIN, ROLE_SUPERADMIN) or bool(user.department_access)


def build_access_filter(user: "User | None") -> dict:
    """Build an OpenSearch bool filter that respects role + department scoping."""
    from app.models import ROLE_SUPERADMIN

    if user is None:
        return {"term": {"status": "public"}}

    if user.role == ROLE_SUPERADMIN:
        return {"match_all": {}}

    # public + employee are company-wide (any authenticated user)
    should_clauses: list[dict] = [
        {"term": {"status": "public"}},
        {"term": {"status": "employee"}},
    ]

    # custom status: only the specific people picked at upload o compartidas después
    should_clauses.append({
        "bool": {
            "must": [
                {"term": {"status": "custom"}},
                {"term": {"allowed_user_ids": str(user.id)}},
            ]
        }
    })

    dept_ids = overseen_department_ids(user)
    if dept_ids:
        # custom status de los departamentos que supervisa (el propio si es
        # admin, más sus accesos extra): aunque no esté entre las personas
        # específicas elegidas.
        should_clauses.append({
            "bool": {
                "must": [
                    {"terms": {"department_id": sorted(str(d) for d in dept_ids)}},
                    {"term": {"status": "custom"}},
                ]
            }
        })

    # Documento dentro de una carpeta compartida conmigo (o con un ancestro
    # de esa carpeta) — independiente del status del documento.
    should_clauses.append({"term": {"folder_shared_user_ids": str(user.id)}})

    return {"bool": {"should": should_clauses, "minimum_should_match": 1}}


def build_shared_with_me_filter(user: "User") -> dict:
    """OpenSearch filter for documents explicitly shared with this user —
    directamente (status='custom' + listado en allowed_user_ids, desde el
    picker al crear o vía "compartir" después) o a través de una carpeta de
    trabajo compartida con esta persona. No incluye lo visible solo por
    status público/empleados ni el auto-acceso de admin a los custom de su
    depto: eso es visibilidad general, no un compartido directo. Usado en
    /documents/mine (pestaña "compartidos")."""
    return {
        "bool": {
            "should": [
                {
                    "bool": {
                        "must": [
                            {"term": {"status": "custom"}},
                            {"term": {"allowed_user_ids": str(user.id)}},
                        ]
                    }
                },
                {"term": {"folder_shared_user_ids": str(user.id)}},
            ],
            "minimum_should_match": 1,
        }
    }


def can_access_document(user: "User | None", doc: "Document") -> bool:
    from app.models import ROLE_SUPERADMIN, STATUS_PUBLIC

    if doc.status == STATUS_PUBLIC:
        return True
    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if doc.status == "employee":
        return True  # any authenticated user, company-wide
    if doc.status == "custom":
        if doc.department_id in overseen_department_ids(user):
            return True  # admin del depto o con acceso extra: lo supervisa
        if any(str(au.user_id) == str(user.id) for au in doc.allowed_users):
            return True
    # Carpeta compartida conmigo (o con un ancestro de esa carpeta), sin
    # importar el status del documento.
    folder = doc.folder
    while folder is not None:
        if any(str(fau.user_id) == str(user.id) for fau in folder.allowed_users):
            return True
        folder = folder.parent
    return False


def can_manage_document(user: "User | None", doc: "Document") -> bool:
    from app.models import ROLE_SUPERADMIN

    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if doc.department_id in managed_department_ids(user):
        return True
    if doc.uploaded_by and str(doc.uploaded_by) == str(user.id):
        return True
    return False


def can_manage_folder(user: "User | None", folder: "Folder") -> bool:
    """Solo el dueño (siempre admin, únicos que crean carpetas) o superadmin."""
    from app.models import ROLE_SUPERADMIN

    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    return str(folder.owner_id) == str(user.id)


def can_access_folder(user: "User | None", folder: "Folder") -> bool:
    from app.models import ROLE_SUPERADMIN

    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(folder.owner_id) == str(user.id):
        return True
    current = folder
    while current is not None:
        if any(str(fau.user_id) == str(user.id) for fau in current.allowed_users):
            return True
        current = current.parent
    return False


def can_enter_folder(user: "User | None", folder: "Folder") -> bool:
    """Como can_access_folder, pero también True cuando la carpeta no está
    compartida completa pero contiene, en cualquier profundidad, al menos un
    documento o subcarpeta a los que el usuario sí tiene acceso individual
    (compartido selectivo) — así puede navegarla como contenedor (al estilo
    Drive) aunque solo vea una parte de lo que hay dentro. `folder_detail`
    usa esto para decidir si deja entrar, y luego filtra lo que se muestra."""
    if can_access_folder(user, folder):
        return True
    if user is None:
        return False
    if any(can_access_document(user, doc) for doc in folder.documents):
        return True
    return any(can_enter_folder(user, child) for child in folder.children)


def can_manage_user(actor: "User", target: "User") -> bool:
    """Who can disable/reset-password another user."""
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_AUDITOR

    if str(actor.id) == str(target.id):
        return False  # nobody manages themselves here
    if actor.role == ROLE_SUPERADMIN:
        return True  # superadmin manages everyone
    if actor.role == ROLE_ADMIN:
        # admin manages only employees (auditores incluidos) in their own department;
        # en un departamento acotado por sucursal (Comercial), solo los de sus sucursales.
        if target.role not in (ROLE_EMPLOYEE, ROLE_AUDITOR) or not actor.department_id:
            return False
        if str(target.department_id) != str(actor.department_id):
            return False
        branch_ids = managed_branch_ids(actor)
        return branch_ids is None or target.branch_id in branch_ids
    return False


def visible_tasks_query(user: "User", db):
    """SQLAlchemy query for tasks visible to this user, with eager loads."""
    from sqlalchemy import or_
    from sqlalchemy.orm import joinedload
    from app.models import Task, ROLE_SUPERADMIN, ROLE_AUDITOR

    q = db.query(Task).options(
        joinedload(Task.assignee),
        joinedload(Task.created_by_user),
        joinedload(Task.department),
        joinedload(Task.project),
    )
    if user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR):
        return q
    conditions = [
        Task.created_by == user.id,
        Task.assigned_to == user.id,
    ]
    if user.department_id:
        conditions.append(Task.department_id == user.department_id)
    granted = granted_department_ids(user)
    if granted:
        conditions.append(Task.department_id.in_(granted))
    return q.filter(or_(*conditions))


def can_manage_doc_dict(user: "User | None", doc_dict: dict) -> bool:
    """Same logic but for OpenSearch result dicts (used in list view)."""
    from app.models import ROLE_SUPERADMIN

    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if doc_dict.get("department_id") in {str(d) for d in managed_department_ids(user)}:
        return True
    if doc_dict.get("uploaded_by") and doc_dict.get("uploaded_by") == str(user.id):
        return True
    return False


def manager_scope(user: "User", db) -> dict | None:
    """Alcance de gestión del trabajo (tareas, plantillas) de un encargado.

    - superadmin → None (sin restricción, todo).
    - `dept_ids`: departamentos completos. Los accesos `manage` y, si es admin,
      el propio — salvo que su departamento sea `branch_scoped` (Comercial) y su
      puesto no cubra todas las sucursales (coordinadores, supervisores).
    - `branch_ids` + `branch_dept_id`: en ese caso, solo la gente de su
      departamento en sus sucursales (UserBranch, sincronizado de hoss).
    """
    from app.models import ACCESS_MANAGE, ROLE_ADMIN, ROLE_SUPERADMIN

    if user.role == ROLE_SUPERADMIN:
        return None

    scope = {"dept_ids": granted_department_ids(user, ACCESS_MANAGE), "branch_ids": set(), "branch_dept_id": None}
    if user.role == ROLE_ADMIN and user.department_id:
        branch_ids = managed_branch_ids(user)
        if branch_ids is None:
            scope["dept_ids"].add(user.department_id)
        else:
            scope["branch_ids"] = branch_ids
            scope["branch_dept_id"] = user.department_id
    return scope


def scope_user_condition(scope: dict):
    """Condición SQL sobre User de las personas dentro del alcance, o None si
    el alcance está vacío."""
    from sqlalchemy import and_, or_
    from app.models import User

    conds = []
    if scope["dept_ids"]:
        conds.append(User.department_id.in_(scope["dept_ids"]))
    if scope["branch_ids"]:
        conds.append(and_(User.branch_id.in_(scope["branch_ids"]), User.department_id == scope["branch_dept_id"]))
    return or_(*conds) if conds else None


def branch_user_ids(scope: dict | None, db) -> set:
    """IDs de las personas de su departamento en las sucursales del alcance."""
    from app.models import User

    if not scope or not scope["branch_ids"]:
        return set()
    return {
        row[0] for row in db.query(User.id).filter(
            User.branch_id.in_(scope["branch_ids"]), User.department_id == scope["branch_dept_id"],
        ).all()
    }


def visible_projects_filter(user: "User", db):
    """Condición SQL de proyectos visibles, o None si no hay restricción
    (superadmin): los que creó, los de su departamento, sucursal y
    departamentos con acceso extra, y en un departamento `branch_scoped` los de
    sus sucursales y las zonas de esas sucursales."""
    from sqlalchemy import or_
    from app.models import Branch, Project, ROLE_SUPERADMIN

    if user.role == ROLE_SUPERADMIN:
        return None

    conditions = [Project.created_by == user.id]

    if user.department_id:
        conditions.append(Project.department_id == user.department_id)
    if user.branch_id:
        conditions.append(Project.branch_id == user.branch_id)

    granted = granted_department_ids(user)
    if granted:
        conditions.append(Project.department_id.in_(granted))

    if user.department and user.department.branch_scoped:
        branch_ids = [ub.branch_id for ub in user.user_branches]
        if branch_ids:
            conditions.append(Project.branch_id.in_(branch_ids))
            zone_ids = {
                row[0] for row in db.query(Branch.zone_id)
                .filter(Branch.id.in_(branch_ids), Branch.zone_id.isnot(None)).distinct().all()
            }
            if zone_ids:
                conditions.append(Project.zone_id.in_(zone_ids))

    return or_(*conditions)


def visible_projects_query(user: "User", db):
    """Proyectos visibles para el usuario, con sus relaciones precargadas."""
    from sqlalchemy.orm import joinedload
    from app.models import Project

    q = db.query(Project).options(
        joinedload(Project.department),
        joinedload(Project.branch),
        joinedload(Project.zone),
        joinedload(Project.created_by_user),
        joinedload(Project.tasks),
    )
    cond = visible_projects_filter(user, db)
    return q if cond is None else q.filter(cond)


def calendar_members_query(viewer: "User", db):
    """Usuarios cuyo calendario puede agregar `viewer` a su vista.

    - superadmin → cualquier persona activa.
    - admin → employees y auditores de su alcance (manager_scope: su
      departamento, o en Comercial las de sus sucursales).
    - accesos extra (view o manage) → cualquier persona de esos departamentos.
    - sin nada de lo anterior → nadie (solo su propio calendario).

    Se evalúa en cada consulta, no solo al suscribirse: si alguien cambia de
    departamento o sucursal, la suscripción deja de mostrarlo sola.
    """
    from sqlalchemy import and_, false, or_
    from app.models import User, ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_AUDITOR

    q = db.query(User).filter(User.is_active.is_(True), User.id != viewer.id)
    if viewer.role == ROLE_SUPERADMIN:
        return q

    conds = []
    granted = granted_department_ids(viewer)
    if granted:
        conds.append(User.department_id.in_(granted))
    if viewer.role == ROLE_ADMIN:
        own = manager_scope(viewer, db)
        own["dept_ids"] = own["dept_ids"] - granted
        in_scope = scope_user_condition(own)
        if in_scope is not None:
            conds.append(and_(in_scope, User.role.in_((ROLE_EMPLOYEE, ROLE_AUDITOR))))
    return q.filter(or_(*conds)) if conds else q.filter(false())


def calendar_department_ids(viewer: "User", db) -> set | None:
    """Departamentos que `viewer` puede agregar completos. None = todos
    (superadmin). Son los de las personas que puede agregar; al agregar uno
    solo se ven las de su alcance (en Comercial, las de sus sucursales)."""
    from app.models import User, ROLE_SUPERADMIN

    if viewer.role == ROLE_SUPERADMIN:
        return None
    return {
        row[0] for row in calendar_members_query(viewer, db)
        .filter(User.department_id.isnot(None))
        .with_entities(User.department_id).distinct().all()
    }
