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
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_EMPLOYEE

    if str(actor.id) == str(target.id):
        return False  # nobody manages themselves here
    if actor.role == ROLE_SUPERADMIN:
        return True  # superadmin manages everyone
    if actor.role == ROLE_ADMIN:
        # admin manages only employees in their own department
        return (
            target.role == ROLE_EMPLOYEE
            and str(target.department_id) == str(actor.department_id)
        )
    return False


def visible_tasks_query(user: "User", db):
    """SQLAlchemy query for tasks visible to this user, with eager loads."""
    from sqlalchemy import or_
    from sqlalchemy.orm import joinedload
    from app.models import Task, ROLE_SUPERADMIN

    q = db.query(Task).options(
        joinedload(Task.assignee),
        joinedload(Task.created_by_user),
        joinedload(Task.department),
        joinedload(Task.project),
    )
    if user.role == ROLE_SUPERADMIN:
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
