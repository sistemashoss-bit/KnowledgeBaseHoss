"""Funciones compartidas por las rutas de tareas (app/tasks/routes/)."""

import re
import uuid
from datetime import date, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from app import rag
from app.messaging import realtime
from app.permissions import build_access_filter
from app.models import (
    Task,
    TaskComment,
    TaskFollowUp,
    TaskShare,
    TaskTag,
    User,
    ROLE_SUPERADMIN,
    TASK_STATUSES,
    TASK_DONE,
)
from app.calendar.reminders import to_local
from app.tasks.business_hours import business_hours_label, business_seconds
from app.tasks.service import _can_edit_task, _is_shared_with, _tasks_query


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:200] or "file"


def _can_update_status(user: User, task: Task, db: Session) -> bool:
    if _can_edit_task(user, task, db):
        return True
    return task.assigned_to and str(task.assigned_to) == str(user.id)


def _can_approve_task(user: User, task: Task) -> bool:
    """Sólo quien asignó la tarea (su creador) o un superadmin puede aprobarla
    (marcarla Lista). El asignado puede llevarla hasta Revisión, no más allá."""
    if user.role == ROLE_SUPERADMIN:
        return True
    return str(task.created_by) == str(user.id)


def _can_comment(user: User, task_id, db: Session) -> bool:
    """Comenta quien participa por su alcance o a quien se la compartieron (el
    auditor fuera de su alcance solo lee)."""
    return (
        _tasks_query(user, db).filter(Task.id == task_id).first() is not None
        or _is_shared_with(user, task_id, db)
    )


def _can_share(user: User, task: Task, db: Session) -> bool:
    """Comparte quien puede editarla y el asignado; a quien se la compartieron, no."""
    return _can_edit_task(user, task, db) or (task.assigned_to and str(task.assigned_to) == str(user.id))


def _followers(task: Task) -> set:
    """Quienes reciben los avisos de la tarea: asignado, creador y con quienes se compartió."""
    return {task.assigned_to, task.created_by} | {s.user_id for s in task.shares}


def _share_candidates(user: User, db: Session) -> list[User]:
    """Cualquier persona activa de cualquier departamento (el selector filtra por
    departamento y nombre en el cliente)."""
    return (
        db.query(User).options(joinedload(User.department))
        .filter(User.is_active == True, User.id != user.id)  # noqa: E712
        .order_by(User.name, User.email).all()
    )


def _add_shares(task: Task, user_ids: list[str], actor: User, db: Session) -> list:
    """Agrega a quienes falten (activos; ni el creador ni el asignado, que ya la
    siguen). Devuelve los ids agregados para avisarles tras el commit."""
    wanted = {uid for uid in user_ids if uid}
    if not wanted:
        return []
    existing = {str(s.user_id) for s in db.query(TaskShare.user_id).filter(TaskShare.task_id == task.id).all()}
    skip = existing | {str(task.created_by), str(task.assigned_to), str(actor.id)}
    try:
        valid = {
            row[0] for row in db.query(User.id).filter(
                User.id.in_([uuid.UUID(uid) for uid in wanted - skip]), User.is_active == True,  # noqa: E712
            ).all()
        } if wanted - skip else set()
    except ValueError:
        raise HTTPException(400, "Persona inválida")
    for uid in valid:
        db.add(TaskShare(task_id=task.id, user_id=uid, shared_by=actor.id))
    return list(valid)


def _notify_shared(task: Task, user_ids: list, actor: User) -> None:
    name = actor.name or actor.email
    for uid in user_ids:
        realtime.notify_user(
            uid, title=f"{name} compartió una tarea contigo", body=task.title,
            url=f"/tasks/{task.id}", kind="task_shared",
        )


def _status_durations(task: Task) -> dict:
    """Tiempo acumulado en cada estado (sin contar "Listo", que es terminal y no
    aporta al desglose) más el tiempo total que tardó la tarea, de punta a punta.
    Ambos en horas laborales (app/tasks/business_hours.py), no de reloj.

    El tramo del estado actual se cuenta hasta ahora, así que el desglose siempre
    refleja algo aunque la tarea siga abierta. El total, en cambio, sólo tiene
    sentido una vez la tarea se aprobó (pasó a "Listo"); mientras no llegue a ese
    estado, `total_seconds` es None. `archived_at` no participa: solo indica que
    se sacó del Kanban (siempre después de "Listo"), no cuándo se completó.
    """
    history = task.status_history
    if not history:
        return {"breakdown": [], "total_seconds": None}
    totals = {s: 0.0 for s in TASK_STATUSES}
    now_end = datetime.utcnow()
    for i, entry in enumerate(history):
        segment_end = history[i + 1].changed_at if i + 1 < len(history) else now_end
        seconds = business_seconds(entry.changed_at, segment_end)
        if entry.to_status in totals and seconds > 0:
            totals[entry.to_status] += seconds

    finished_at = history[-1].changed_at if task.status == TASK_DONE else None

    return {
        "breakdown": [
            {"status": s, "seconds": totals[s]}
            for s in TASK_STATUSES if s != TASK_DONE and totals[s] > 0
        ],
        "total_seconds": business_seconds(history[0].changed_at, finished_at) if finished_at else None,
        "hours_label": business_hours_label(),
    }


def _parse_date(value: str, error: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        raise HTTPException(422, error)


def _accessible_documents(user: User) -> list[dict]:
    """Documentos que el usuario puede referenciar en una tarea (access-filtered).

    Reusa la búsqueda con el filtro de visibilidad del módulo Documentos, así que
    respeta rol/departamento. Devuelve dicts con id/title/department_name/content_type.
    """
    try:
        return rag.search_documents("", build_access_filter(user), size=500)
    except Exception:
        return []


def _validated_tag_ids(tag_ids: list[str], department_id: str | None, db: Session) -> list[str]:
    """Filtra a los ids de etiquetas que existen y pertenecen al departamento dado.
    Una tarea sin departamento no puede llevar etiquetas (son siempre por depto)."""
    tag_ids = [t for t in dict.fromkeys(tag_ids) if t]
    if not tag_ids or not department_id:
        return []
    valid = {
        str(row[0]) for row in db.query(TaskTag.id).filter(
            TaskTag.department_id == department_id, TaskTag.id.in_(tag_ids)
        ).all()
    }
    return [t for t in tag_ids if t in valid]


def _comment_threads(comments: list[TaskComment]) -> list[tuple[TaskComment, list[TaskComment]]]:
    """[(raíz, respuestas)] en orden cronológico. Una respuesta cuyo raíz ya no
    está (borrado → SET NULL, o fuera de la lista) se muestra como raíz."""
    ids = {c.id for c in comments}
    replies: dict = {}
    for c in comments:
        if c.parent_id and c.parent_id in ids:
            replies.setdefault(c.parent_id, []).append(c)
    return [(c, replies.get(c.id, [])) for c in comments if not (c.parent_id and c.parent_id in ids)]


def _follow_up_view(f: TaskFollowUp) -> dict:
    """Datos para pintar un seguimiento en el detalle (horas en hora local)."""
    start, end = to_local(f.start_at), to_local(f.end_at)
    return {
        "f": f,
        "date": start.date().isoformat(),
        "start": f"{start:%H:%M}",
        "end": f"{end:%H:%M}",
        "label": f"{start:%d/%m/%Y} · {start:%H:%M}–{end:%H:%M}",
        "past": f.end_at < datetime.utcnow(),
    }
