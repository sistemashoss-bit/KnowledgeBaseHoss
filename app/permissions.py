from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models import Document, User


def build_access_filter(user: "User | None") -> dict:
    """Build an OpenSearch bool filter that respects role + department scoping."""
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN

    if user is None:
        return {"term": {"status": "public"}}

    if user.role == ROLE_SUPERADMIN:
        return {"match_all": {}}

    # employee status is company-wide (any authenticated user)
    should_clauses: list[dict] = [
        {"term": {"status": "public"}},
        {"term": {"status": "employee"}},
    ]

    # admin status is still scoped to the user's own department
    if user.role == ROLE_ADMIN:
        dept_id = str(user.department_id) if user.department_id else "__none__"
        should_clauses.append({
            "bool": {
                "must": [
                    {"term": {"department_id": dept_id}},
                    {"term": {"status": "admin"}},
                ]
            }
        })

    return {"bool": {"should": should_clauses, "minimum_should_match": 1}}


def can_access_document(user: "User | None", doc: "Document") -> bool:
    from app.models import ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_EMPLOYEE, STATUS_PUBLIC

    if doc.status == STATUS_PUBLIC:
        return True
    if user is None:
        return False
    if user.role == ROLE_SUPERADMIN:
        return True
    if doc.status == "employee":
        return True  # any authenticated user
    if doc.status == "admin":
        return user.role in (ROLE_ADMIN, ROLE_SUPERADMIN) and str(doc.department_id) == str(user.department_id)
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
