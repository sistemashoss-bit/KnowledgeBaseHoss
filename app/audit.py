"""
Non-blocking audit/search log helpers.
Each function opens its own session so log failures never roll back the caller.
"""
import logging
import uuid

logger = logging.getLogger(__name__)


def log_action(
    action: str,
    *,
    user=None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_name: str | None = None,
    details: str | None = None,
    request=None,
) -> None:
    try:
        from app.database import SessionLocal
        from app.models import AuditLog

        ip = None
        if request is not None:
            ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or None

        db = SessionLocal()
        try:
            db.add(AuditLog(
                id=uuid.uuid4(),
                user_id=user.id if user else None,
                user_email=user.email if user else None,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id) if resource_id else None,
                resource_name=resource_name,
                details=details,
                ip_address=ip,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.error("audit log failed: %s", exc)


def log_search(
    query: str,
    *,
    user=None,
    result_count: int = 0,
    search_type: str = "document",
) -> None:
    if not query or not query.strip():
        return
    try:
        from app.database import SessionLocal
        from app.models import SearchLog

        db = SessionLocal()
        try:
            db.add(SearchLog(
                id=uuid.uuid4(),
                user_id=user.id if user else None,
                user_email=user.email if user else None,
                query=query.strip(),
                result_count=result_count,
                search_type=search_type,
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        logger.error("search log failed: %s", exc)
