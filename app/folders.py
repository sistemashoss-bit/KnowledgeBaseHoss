from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import Folder


def ancestor_chain(db: Session, folder_id) -> list["Folder"]:
    """La carpeta y todos sus ancestros, del más específico al raíz."""
    from app.models import Folder

    chain: list[Folder] = []
    current = db.query(Folder).filter(Folder.id == folder_id).first()
    while current is not None:
        chain.append(current)
        current = (
            db.query(Folder).filter(Folder.id == current.parent_id).first()
            if current.parent_id else None
        )
    return chain


def effective_shared_user_ids(db: Session, folder_id) -> set[str]:
    """Unión de los compartidos de esta carpeta y de todos sus ancestros:
    compartir una carpeta padre da acceso a todo su subárbol."""
    from app.models import FolderAllowedUser

    ids: set[str] = set()
    for folder in ancestor_chain(db, folder_id):
        for fau in db.query(FolderAllowedUser).filter(FolderAllowedUser.folder_id == folder.id).all():
            ids.add(str(fau.user_id))
    return ids


def descendant_folder_ids(db: Session, folder_id) -> list:
    """La carpeta y todas sus subcarpetas (recursivo), como lista de ids."""
    from app.models import Folder

    result = [folder_id]
    pending = [folder_id]
    while pending:
        current = pending.pop()
        children = db.query(Folder.id).filter(Folder.parent_id == current).all()
        for (child_id,) in children:
            result.append(child_id)
            pending.append(child_id)
    return result


def reindex_folder_subtree(db: Session, folder_id, background_tasks) -> None:
    """Tras compartir/revocar una carpeta: recalcula folder_shared_user_ids
    para cada documento del subárbol (la propia carpeta + subcarpetas) y
    reindexa. Se usa también al mover un documento a una carpeta."""
    from app import rag
    from app.models import Department, Document

    folder_ids = descendant_folder_ids(db, folder_id)
    docs = db.query(Document).filter(Document.folder_id.in_(folder_ids)).all()
    for doc in docs:
        shared_ids = sorted(effective_shared_user_ids(db, doc.folder_id))
        allowed_user_ids = [str(au.user_id) for au in doc.allowed_users]
        dept = db.query(Department).filter(Department.id == doc.department_id).first()
        background_tasks.add_task(
            rag.update_document_metadata,
            doc_id=str(doc.id),
            title=doc.title,
            description=doc.description,
            department_id=str(doc.department_id),
            department_name=dept.name if dept else "",
            status=doc.status,
            content_type=doc.content_type or "",
            uploaded_by=str(doc.uploaded_by),
            allowed_user_ids=allowed_user_ids,
            is_work_document=doc.is_work_document,
            folder_shared_user_ids=shared_ids,
        )
