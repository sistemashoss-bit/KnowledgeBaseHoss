from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.deps import require_role
from app.database import get_db
from app.models import AuditLog, ROLE_SUPERADMIN, SearchLog
from app.templating import templates

router = APIRouter(prefix="/logs", tags=["logs"])

_PAGE = 100


@router.get("/audit", response_class=HTMLResponse)
def audit_logs(
    request: Request,
    page: int = 1,
    action: str = "",
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    q = db.query(AuditLog)
    if action:
        q = q.filter(AuditLog.action == action)
    total = q.count()
    logs = q.order_by(AuditLog.created_at.desc()).offset((page - 1) * _PAGE).limit(_PAGE).all()
    actions = [r[0] for r in db.query(AuditLog.action).distinct().all()]

    return templates.TemplateResponse(
        request, "logs/audit.html",
        {
            "logs": logs,
            "current_user": user,
            "page": page,
            "total": total,
            "page_size": _PAGE,
            "actions": sorted(actions),
            "selected_action": action,
        },
    )


@router.get("/searches", response_class=HTMLResponse)
def search_logs(
    request: Request,
    page: int = 1,
    search_type: str = "",
    db: Session = Depends(get_db),
    user=Depends(require_role(ROLE_SUPERADMIN)),
):
    q = db.query(SearchLog)
    if search_type:
        q = q.filter(SearchLog.search_type == search_type)
    total = q.count()
    logs = q.order_by(SearchLog.created_at.desc()).offset((page - 1) * _PAGE).limit(_PAGE).all()

    return templates.TemplateResponse(
        request, "logs/searches.html",
        {
            "logs": logs,
            "current_user": user,
            "page": page,
            "total": total,
            "page_size": _PAGE,
            "selected_type": search_type,
        },
    )
