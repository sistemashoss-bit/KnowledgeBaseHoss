from collections import defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth.deps import get_current_user
from app.database import get_db
from app.models import (
    AuditLog, Branch, Department, Project, SearchLog,
    Task, User, UserZone, Zone,
    ROLE_SUPERADMIN, ROLE_ADMIN,
    TASK_STATUSES, TASK_PRIORITIES, PROJECT_STATUSES,
)
from app.templating import templates

router = APIRouter(prefix="/reports", tags=["reports"])


# ── Scope helpers ─────────────────────────────────────────────────────────────

def _dept_ids_for_zone(zone_id: str, db: Session) -> list:
    branch_ids = [b.id for b in db.query(Branch).filter(Branch.zone_id == zone_id).all()]
    if not branch_ids:
        return []
    return [d.id for d in db.query(Department).filter(Department.branch_id.in_(branch_ids)).all()]


def _resolve_scope(current_user, zone_id: str, department_id: str, db: Session):
    """
    Returns (allowed_dept_ids, forced_zone_id, forced_dept_id).
    allowed_dept_ids=None means no restriction (superadmin, all).
    """
    if current_user.role == ROLE_SUPERADMIN:
        if zone_id:
            ids = _dept_ids_for_zone(zone_id, db)
            return ids, zone_id, ""
        if department_id:
            return [department_id], "", department_id
        return None, "", ""

    if current_user.role == ROLE_ADMIN:
        dept_id = str(current_user.department_id) if current_user.department_id else None
        return ([dept_id] if dept_id else []), "", dept_id or ""

    # employees with zone assignments
    zone_ids = [str(uz.zone_id) for uz in db.query(UserZone).filter(UserZone.user_id == current_user.id).all()]
    if zone_ids:
        ids = []
        for zid in zone_ids:
            ids.extend(_dept_ids_for_zone(zid, db))
        return ids, "", ""

    # employee with only department
    if current_user.department_id:
        return [str(current_user.department_id)], "", str(current_user.department_id)

    return [], "", ""


# ── Data helpers ──────────────────────────────────────────────────────────────

def _task_stats(dept_ids, dt_from: datetime, dt_to: datetime, db: Session) -> dict:
    q = db.query(Task).filter(Task.created_at >= dt_from, Task.created_at < dt_to)
    if dept_ids is not None:
        q = q.filter(Task.department_id.in_(dept_ids)) if dept_ids else q.filter(Task.id == None)
    tasks = q.all()

    today_str = date.today().isoformat()
    by_status = defaultdict(int)
    by_priority = defaultdict(int)
    for t in tasks:
        by_status[t.status] += 1
        by_priority[t.priority] += 1

    overdue = db.query(func.count(Task.id)).filter(
        Task.due_date < date.today(),
        Task.status != "done",
    )
    if dept_ids is not None:
        overdue = overdue.filter(Task.department_id.in_(dept_ids)) if dept_ids else overdue.filter(Task.id == None)
    overdue_count = overdue.scalar() or 0

    return {
        "total": len(tasks),
        "done": by_status["done"],
        "in_progress": by_status["in_progress"],
        "pending": by_status["pending"],
        "review": by_status["review"],
        "overdue": overdue_count,
        "by_status": dict(by_status),
        "by_priority": dict(by_priority),
    }


def _tasks_over_time(dept_ids, dt_from: datetime, dt_to: datetime, db: Session) -> list[dict]:
    """Returns list of {date, created, completed} for each day in range."""
    q = db.query(Task).filter(Task.created_at >= dt_from, Task.created_at < dt_to)
    if dept_ids is not None:
        q = q.filter(Task.department_id.in_(dept_ids)) if dept_ids else q.filter(Task.id == None)
    tasks = q.all()

    created_by_day: dict[str, int] = defaultdict(int)
    done_by_day: dict[str, int] = defaultdict(int)
    for t in tasks:
        created_by_day[t.created_at.date().isoformat()] += 1
        if t.status == "done" and t.updated_at:
            done_by_day[t.updated_at.date().isoformat()] += 1

    days = []
    cur = dt_from.date()
    end = dt_to.date()
    while cur < end:
        s = cur.isoformat()
        days.append({"date": s, "created": created_by_day[s], "completed": done_by_day[s]})
        cur += timedelta(days=1)
    return days


def _project_stats(dept_ids, dt_from: datetime, dt_to: datetime, db: Session) -> dict:
    q = db.query(Project).filter(Project.created_at >= dt_from, Project.created_at < dt_to)
    if dept_ids is not None:
        q = q.filter(Project.department_id.in_(dept_ids)) if dept_ids else q.filter(Project.id == None)
    projects = q.all()

    by_status = defaultdict(int)
    for p in projects:
        by_status[p.status] += 1

    # active total (not date-filtered)
    active_q = db.query(func.count(Project.id)).filter(Project.status == "active")
    if dept_ids is not None:
        active_q = active_q.filter(Project.department_id.in_(dept_ids)) if dept_ids else active_q.filter(Project.id == None)
    active_total = active_q.scalar() or 0

    return {
        "total": len(projects),
        "active_total": active_total,
        "by_status": dict(by_status),
    }


def _top_users(dt_from: datetime, dt_to: datetime, db: Session, limit: int = 10) -> list:
    rows = (
        db.query(AuditLog.user_email, func.count(AuditLog.id).label("actions"),
                 func.max(AuditLog.created_at).label("last_at"))
        .filter(AuditLog.created_at >= dt_from, AuditLog.created_at < dt_to)
        .filter(AuditLog.user_email.isnot(None))
        .group_by(AuditLog.user_email)
        .order_by(func.count(AuditLog.id).desc())
        .limit(limit)
        .all()
    )
    return [{"email": r.user_email, "actions": r.actions, "last_at": r.last_at} for r in rows]


def _top_searches(dt_from: datetime, dt_to: datetime, db: Session, limit: int = 10) -> list:
    rows = (
        db.query(SearchLog.query, func.count(SearchLog.id).label("count"),
                 func.avg(SearchLog.result_count).label("avg_results"))
        .filter(SearchLog.created_at >= dt_from, SearchLog.created_at < dt_to)
        .group_by(SearchLog.query)
        .order_by(func.count(SearchLog.id).desc())
        .limit(limit)
        .all()
    )
    return [{"query": r.query, "count": r.count, "avg_results": round(r.avg_results or 0, 1)} for r in rows]


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def reports_dashboard(
    request: Request,
    zone_id: str = "",
    department_id: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    if not current_user:
        return RedirectResponse("/auth/login", status_code=302)
    if current_user.role == ROLE_ADMIN and not current_user.department_id:
        # Admin without department — nothing to scope
        pass
    elif current_user.role not in (ROLE_SUPERADMIN, ROLE_ADMIN):
        # Employees: only allow if they have zone assignments
        has_zones = db.query(UserZone).filter(UserZone.user_id == current_user.id).first()
        if not has_zones and not current_user.department_id:
            raise HTTPException(403)

    # Date range defaults: last 30 days
    try:
        dt_from = datetime.fromisoformat(date_from) if date_from else datetime.utcnow() - timedelta(days=30)
    except ValueError:
        dt_from = datetime.utcnow() - timedelta(days=30)
    try:
        dt_to = datetime.fromisoformat(date_to) + timedelta(days=1) if date_to else datetime.utcnow() + timedelta(days=1)
    except ValueError:
        dt_to = datetime.utcnow() + timedelta(days=1)

    dept_ids, active_zone, active_dept = _resolve_scope(current_user, zone_id, department_id, db)

    # Aggregate
    task_stats = _task_stats(dept_ids, dt_from, dt_to, db)
    project_stats = _project_stats(dept_ids, dt_from, dt_to, db)
    tasks_timeline = _tasks_over_time(dept_ids, dt_from, dt_to, db)
    top_users = _top_users(dt_from, dt_to, db)
    top_searches = _top_searches(dt_from, dt_to, db)

    # Chart.js data
    timeline_labels = [d["date"] for d in tasks_timeline]
    timeline_created = [d["created"] for d in tasks_timeline]
    timeline_completed = [d["completed"] for d in tasks_timeline]

    status_labels = ["Pendiente", "En progreso", "Revisión", "Listo"]
    status_keys = ["pending", "in_progress", "review", "done"]
    status_data = [task_stats["by_status"].get(k, 0) for k in status_keys]

    priority_labels = ["Baja", "Media", "Alta", "Urgente"]
    priority_keys = ["low", "medium", "high", "urgent"]
    priority_data = [task_stats["by_priority"].get(k, 0) for k in priority_keys]

    # Filter options for superadmin
    zones = db.query(Zone).order_by(Zone.name).all() if current_user.role == ROLE_SUPERADMIN else []
    departments = db.query(Department).order_by(Department.name).all() if current_user.role == ROLE_SUPERADMIN else []

    return templates.TemplateResponse(
        request,
        "reports/dashboard.html",
        {
            "current_user": current_user,
            # filters
            "zones": zones,
            "departments": departments,
            "active_zone": active_zone,
            "active_dept": active_dept,
            "date_from": date_from or (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d"),
            "date_to": date_to or date.today().isoformat(),
            # KPIs
            "task_stats": task_stats,
            "project_stats": project_stats,
            # tables
            "top_users": top_users,
            "top_searches": top_searches,
            # chart data (JSON-safe)
            "timeline_labels": timeline_labels,
            "timeline_created": timeline_created,
            "timeline_completed": timeline_completed,
            "status_labels": status_labels,
            "status_data": status_data,
            "priority_labels": priority_labels,
            "priority_data": priority_data,
        },
    )
