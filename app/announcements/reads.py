"""Confirmación de visto de los comunicados.

- Visto: se registra la primera vez que la persona abre el comunicado
  (/documents/{id}/view o /download); vale para todos los comunicados.
- Enterado: solo si el comunicado lo pide (Document.requires_ack); la persona
  lo confirma con el botón y queda como constancia con fecha.

Los destinatarios (de quienes se espera el visto) son los mismos a quienes
se avisa al publicar: todos los activos si es público o de empleados; si es
de personas específicas, esas personas (incluidas las que se agregaron con
"Compartir"). Nunca el autor ni los bots. Quien puede verlo sin ser
destinatario (p. ej. el admin que supervisa el depto) puede abrirlo, pero no
cuenta en el "visto por X de Y".
"""
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    DOC_KIND_ANNOUNCEMENT, STATUS_CUSTOM, AnnouncementRead, Document, DocumentAllowedUser, User,
)


def audience_query(db: Session, doc: Document):
    """Consulta de User (activos, no bots, sin el autor) a quienes va el comunicado."""
    q = db.query(User).filter(User.is_active == True, User.is_bot == False)  # noqa: E712
    if doc.uploaded_by:
        q = q.filter(User.id != doc.uploaded_by)
    if doc.status == STATUS_CUSTOM:
        q = q.filter(User.id.in_(
            db.query(DocumentAllowedUser.user_id).filter(DocumentAllowedUser.document_id == doc.id)
        ))
    return q


def is_recipient(doc: Document, user: User | None) -> bool:
    """Si a esta persona le toca ver (y confirmar) el comunicado: el autor no."""
    return bool(user) and not user.is_bot and str(doc.uploaded_by) != str(user.id)


def mark_seen(db: Session, doc: Document, user: User | None) -> None:
    """Primera apertura; las siguientes no cambian la fecha."""
    if doc.kind != DOC_KIND_ANNOUNCEMENT or not is_recipient(doc, user):
        return
    now = datetime.utcnow()
    db.execute(
        insert(AnnouncementRead)
        .values(document_id=doc.id, user_id=user.id, seen_at=now)
        .on_conflict_do_update(
            index_elements=["document_id", "user_id"],
            set_={"seen_at": func.coalesce(AnnouncementRead.seen_at, now)},
        )
    )
    db.commit()


def acknowledge(db: Session, doc: Document, user: User) -> None:
    """"Enterado" (cuenta también como visto). Repetirlo no cambia la fecha."""
    now = datetime.utcnow()
    db.execute(
        insert(AnnouncementRead)
        .values(document_id=doc.id, user_id=user.id, seen_at=now, acknowledged_at=now)
        .on_conflict_do_update(
            index_elements=["document_id", "user_id"],
            set_={
                "seen_at": func.coalesce(AnnouncementRead.seen_at, now),
                "acknowledged_at": func.coalesce(AnnouncementRead.acknowledged_at, now),
            },
        )
    )
    db.commit()


def my_reads(db: Session, user: User | None, doc_ids: list[str]) -> dict[str, AnnouncementRead]:
    """{doc_id: fila} de esta persona para el listado."""
    if not user or not doc_ids:
        return {}
    rows = db.query(AnnouncementRead).filter(
        AnnouncementRead.user_id == user.id, AnnouncementRead.document_id.in_(doc_ids),
    )
    return {str(r.document_id): r for r in rows}


def stats(db: Session, doc: Document) -> dict:
    """{"total", "seen", "acked"} contando solo a los destinatarios."""
    audience = audience_query(db, doc).with_entities(User.id).subquery()
    total = db.query(func.count()).select_from(audience).scalar() or 0
    seen, acked = db.query(
        func.count(AnnouncementRead.seen_at), func.count(AnnouncementRead.acknowledged_at),
    ).filter(
        AnnouncementRead.document_id == doc.id, AnnouncementRead.user_id.in_(db.query(audience.c.id)),
    ).one()
    return {"total": total, "seen": seen or 0, "acked": acked or 0}


def detail(db: Session, doc: Document) -> list[dict]:
    """Cada destinatario con su visto/enterado, para la página de quien publica."""
    rows = (
        audience_query(db, doc)
        .outerjoin(AnnouncementRead, (AnnouncementRead.user_id == User.id) & (AnnouncementRead.document_id == doc.id))
        .with_entities(User, AnnouncementRead.seen_at, AnnouncementRead.acknowledged_at)
        .all()
    )
    out = [{"user": u, "seen_at": seen, "acknowledged_at": ack} for u, seen, ack in rows]
    out.sort(key=lambda r: ((r["user"].name or r["user"].email or "").lower()))
    return out
