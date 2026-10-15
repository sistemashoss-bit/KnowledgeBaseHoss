from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Document, Folder, User


def build_access_filter(user: "User | None") -> dict:
    """Build an OpenSearch bool filter that respects role + department scoping."""
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN

    if user is None:
        return {"term": {"status": "public"}}

    if user.role == ROLE_SUPERADMIN:
        return {"match_all": {}}

    # public + employee are company-wide (any authenticated user)
    should_clauses: list[dict] = [
        {"term": {"status": "public"}},
        {"term": {"status": "employee"}},
    ]

    dept_id = str(user.department_id) if user.department_id else "__none__"

    # custom status: only the specific people picked at upload o compartidas después
    should_clauses.append({
        "bool": {
            "must": [
                {"term": {"status": "custom"}},
                {"term": {"allowed_user_ids": str(user.id)}},
            ]
        }
    })

    if user.role == ROLE_ADMIN:
        # custom status del propio departamento: el admin ya puede gestionarlos
        # (can_manage_document), así que también debe poder verlos aunque no
        # esté entre las personas específicas elegidas.
        should_clauses.append({
            "bool": {
                "must": [
                    {"term": {"department_id": dept_id}},
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
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN, STATUS_PUBLIC

    if doc.status == STATUS_PUBLIC:
        return True
    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if doc.status == "employee":
        return True  # any authenticated user, company-wide
    if doc.status == "custom":
        if user.role == ROLE_ADMIN and str(doc.department_id) == str(user.department_id):
            return True  # ya puede gestionarlo (can_manage_document); también debe poder verlo
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
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN

    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if user.role == ROLE_ADMIN and str(doc.department_id) == str(user.department_id):
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
        # admin manages only employees (auditores incluidos) in their own department
        return (
            target.role in (ROLE_EMPLOYEE, ROLE_AUDITOR)
            and str(target.department_id) == str(actor.department_id)
        )
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
    return q.filter(or_(*conditions))


def can_manage_doc_dict(user: "User | None", doc_dict: dict) -> bool:
    """Same logic but for OpenSearch result dicts (used in list view)."""
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN

    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if user.role == ROLE_ADMIN and doc_dict.get("department_id") == str(user.department_id):
        return True
    if doc_dict.get("uploaded_by") and doc_dict.get("uploaded_by") == str(user.id):
        return True
    return False


def manager_scope(user: "User", db) -> dict | None:
    """Alcance de gestión de un encargado.

    - superadmin → None (sin restricción, todo).
    - admin CON zona(s) (coordinador regional) y/o sucursales sueltas
      asignadas directamente (supervisor) → sólo esas sucursales
      (`branch_ids`); no todo el departamento.
    - admin SIN zona ni sucursales (jefe de departamento) → todo su
      departamento (`dept_ids`).

    Devuelve un dict con `dept_ids` y `branch_ids` (uno de los dos vacío según el
    caso), o None para superadmin.
    """
    from app.models import Branch, ROLE_SUPERADMIN

    if user.role == ROLE_SUPERADMIN:
        return None

    zone_ids = [uz.zone_id for uz in user.user_zones]
    branch_ids = set()
    if zone_ids:
        branch_ids |= {
            row[0] for row in db.query(Branch.id).filter(Branch.zone_id.in_(zone_ids)).all()
        }
    branch_ids |= {ub.branch_id for ub in user.user_branches}
    if branch_ids:
        return {"dept_ids": set(), "branch_ids": branch_ids}

    return {"dept_ids": {user.department_id} if user.department_id else set(), "branch_ids": set()}


def branch_user_ids(branch_ids: set, db) -> set:
    """IDs de usuarios que pertenecen a las sucursales dadas."""
    from app.models import User

    if not branch_ids:
        return set()
    return {row[0] for row in db.query(User.id).filter(User.branch_id.in_(branch_ids)).all()}


def visible_projects_filter(user: "User", db):
    """Condición SQL de proyectos visibles, o None si no hay restricción
    (superadmin): los que creó y los de su departamento, sucursal, zonas o
    sucursales asignadas."""
    from sqlalchemy import or_
    from app.models import Project, UserBranch, UserZone, ROLE_SUPERADMIN

    if user.role == ROLE_SUPERADMIN:
        return None

    conditions = [Project.created_by == user.id]

    if user.department_id:
        conditions.append(Project.department_id == user.department_id)
    if user.branch_id:
        conditions.append(Project.branch_id == user.branch_id)

    zone_ids = [uz.zone_id for uz in db.query(UserZone).filter(UserZone.user_id == user.id).all()]
    if zone_ids:
        conditions.append(Project.zone_id.in_(zone_ids))

    direct_branch_ids = [ub.branch_id for ub in db.query(UserBranch).filter(UserBranch.user_id == user.id).all()]
    if direct_branch_ids:
        conditions.append(Project.branch_id.in_(direct_branch_ids))

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
    - admin sin zona → employees y auditores de su departamento.
    - admin con zona/sucursales → employees y auditores de esas sucursales.
    - employee / auditor → nadie (solo su propio calendario).

    Se evalúa en cada consulta, no solo al suscribirse: si alguien cambia de
    departamento o sucursal, la suscripción deja de mostrarlo sola.
    """
    from sqlalchemy import false
    from app.models import User, ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_EMPLOYEE, ROLE_AUDITOR

    q = db.query(User).filter(User.is_active.is_(True), User.id != viewer.id)
    if viewer.role == ROLE_SUPERADMIN:
        return q
    if viewer.role != ROLE_ADMIN:
        return q.filter(false())

    q = q.filter(User.role.in_((ROLE_EMPLOYEE, ROLE_AUDITOR)))
    scope = manager_scope(viewer, db)
    if scope["branch_ids"]:
        return q.filter(User.branch_id.in_(scope["branch_ids"]))
    if scope["dept_ids"]:
        return q.filter(User.department_id.in_(scope["dept_ids"]))
    return q.filter(false())


def calendar_department_ids(viewer: "User", db) -> set | None:
    """Departamentos que `viewer` puede agregar completos. None = todos
    (superadmin). Para un admin con zona son los departamentos de las personas
    de sus sucursales; al agregar uno solo se ven las de su alcance."""
    from app.models import User, ROLE_SUPERADMIN

    if viewer.role == ROLE_SUPERADMIN:
        return None
    return {
        row[0] for row in calendar_members_query(viewer, db)
        .filter(User.department_id.isnot(None))
        .with_entities(User.department_id).distinct().all()
    }
