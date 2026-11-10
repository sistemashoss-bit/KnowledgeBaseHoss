"""Lógica de tareas y seguimientos sin HTTP: permisos, validación de alcance y
creación. La usan las rutas de app/tasks/router.py y el asistente MAX
(app/assistant), así ambos aplican exactamente las mismas reglas.

Las funciones de creación agregan a la sesión y hacen flush, pero no commit:
quien llama decide cuándo confirmar (la ruta suma etiquetas, evidencias…).
Los avisos van aparte (`notify_*`), después del commit.
"""
import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.calendar.reminders import compute_remind_at, to_local, to_utc
from app.messaging import realtime
from app.models import (
    Document, Project, Task, TaskFollowUp, TaskShare, TaskStatusHistory, TaskTagAssignment, User,
    CALENDAR_REMINDER_OPTIONS, ROLE_AUDITOR, ROLE_SUPERADMIN,
    TASK_DONE, TASK_PRIORITIES, TASK_STATUSES, task_dependencies,
)
from app.permissions import (
    branch_user_ids as _branch_user_ids, can_access_document, granted_department_ids, is_manager,
    manager_scope as _manager_scope, scope_user_condition, visible_projects_filter,
)
from app.tasks.recurring import local_now, matches_recurrence, parse_recurrence


# ── Visibilidad y permisos ────────────────────────────────────────────────────

def _tasks_query(user: User, db: Session, include_archived: bool = True, *, audit: bool = False):
    """Tareas visibles para el usuario. `audit=True` solo en vistas de lectura:
    ahí el auditor ve las de todos los departamentos; para participar
    (comentar, subir evidencias) conserva el alcance normal de empleado."""
    q = db.query(Task).options(
        joinedload(Task.assignee),
        joinedload(Task.created_by_user),
        joinedload(Task.department),
        joinedload(Task.projects),
        joinedload(Task.tags).joinedload(TaskTagAssignment.tag),
    )
    if user.role != ROLE_SUPERADMIN and not (audit and user.role == ROLE_AUDITOR):
        conditions = [
            Task.created_by == user.id,
            Task.assigned_to == user.id,
        ]
        if user.department_id:
            conditions.append(Task.department_id == user.department_id)
        granted = granted_department_ids(user)
        if granted:
            conditions.append(Task.department_id.in_(granted))
        q = q.filter(or_(*conditions))
    if not include_archived:
        q = q.filter(Task.archived_at.is_(None))
    return q


def _effective_department_id(department_id, assigned_to, db: Session):
    """Departamento con el que queda una tarea. Si no se eligió uno pero hay
    asignado (p.ej. autoasignación), hereda el del asignado: sin esto la tarea
    queda con department_id NULL y el admin del área no la ve ni la gestiona."""
    if department_id:
        return department_id
    if assigned_to:
        return db.query(User.department_id).filter(User.id == assigned_to).scalar()
    return None


def _can_edit_task(user: User, task: Task, db: Session) -> bool:
    if user.role == ROLE_SUPERADMIN:
        return True
    if str(task.created_by) == str(user.id):
        return True
    scope = _manager_scope(user, db)
    if task.department_id and task.department_id in scope["dept_ids"]:
        return True
    if task.assigned_to and task.assigned_to in _branch_user_ids(scope, db):
        return True
    return False


def _is_shared_with(user: User, task_id, db: Session) -> bool:
    return db.query(TaskShare).filter(TaskShare.task_id == task_id, TaskShare.user_id == user.id).first() is not None


def shared_with_overseen_condition(user: User, db: Session | None = None):
    """Condición SQL sobre Task: compartidas con alguna persona que `user`
    supervisa en la pestaña Departamento (su alcance de gestión más los
    departamentos con acceso extra). None si no supervisa a nadie; el
    superadmin ya ve todas."""
    scope = _manager_scope(user, db)
    if scope is None:
        return None
    in_scope = scope_user_condition({**scope, "dept_ids": scope["dept_ids"] | granted_department_ids(user)})
    if in_scope is None:
        return None
    return Task.id.in_(select(TaskShare.task_id).join(User, User.id == TaskShare.user_id).where(in_scope))


def _is_shared_with_overseen(user: User, task_id, db: Session) -> bool:
    cond = shared_with_overseen_condition(user, db)
    return cond is not None and db.query(Task.id).filter(Task.id == task_id, cond).first() is not None


def _can_view_task(user: User, task_id, db: Session) -> bool:
    """La ve por su alcance (incluye la lectura de auditor), porque se la
    compartieron o porque se compartió con alguien que supervisa."""
    return (
        _tasks_query(user, db, audit=True).filter(Task.id == task_id).first() is not None
        or _is_shared_with(user, task_id, db)
        or _is_shared_with_overseen(user, task_id, db)
    )


def _can_manage_follow_ups(user: User, task: Task, db: Session) -> bool:
    """Programa y cierra seguimientos quien edita la tarea y su asignado."""
    return _can_edit_task(user, task, db) or (task.assigned_to and str(task.assigned_to) == str(user.id))


def visible_projects(user: User, db: Session):
    """Proyectos a los que `user` puede vincular tareas: los que puede ver."""
    q = db.query(Project)
    cond = visible_projects_filter(user, db)
    return q if cond is None else q.filter(cond)


def _validated_projects(project_ids, user: User, db: Session) -> list[Project]:
    """Proyectos de `project_ids` que existen y `user` puede ver (los demás se ignoran)."""
    ids = []
    for pid in project_ids or []:
        try:
            ids.append(uuid.UUID(str(pid)))
        except ValueError:
            continue
    if not ids:
        return []
    return visible_projects(user, db).filter(Project.id.in_(ids)).order_by(Project.name).all()


def set_task_projects(task: Task, project_ids, user: User, db: Session) -> None:
    """Deja la tarea en los proyectos elegidos entre los que `user` puede ver. Los
    vínculos con proyectos que no ve se conservan: no puede quitarlos sin saberlo."""
    current = list(task.projects)
    visible_now = {
        p.id for p in visible_projects(user, db).filter(Project.id.in_([p.id for p in current]))
    } if current else set()
    hidden = [p for p in current if p.id not in visible_now]
    task.projects = hidden + _validated_projects(project_ids, user, db)


def _validated_document_id(document_id: str, user: User, db: Session) -> str | None:
    """Devuelve el id si el documento existe y el usuario puede accederlo; si no, None."""
    if not document_id:
        return None
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc or not can_access_document(user, doc):
        return None
    return document_id


def _assignable_users_query(user: User, db: Session):
    """Usuarios activos a los que se puede asignar una tarea puntual (persona a persona).

    superadmin → cualquiera. encargado → él mismo y las personas de su alcance
    (manager_scope: su departamento —en Comercial, la gente de sus sucursales—
    y los departamentos con acceso `manage`). employee → sólo él mismo
    (autoasignación)."""
    q = db.query(User).filter(User.is_active == True)
    if user.role == ROLE_SUPERADMIN:
        return q.order_by(User.email)
    if is_manager(user):
        in_scope = scope_user_condition(_manager_scope(user, db))
        if in_scope is None:
            return q.filter(User.id == user.id)
        return q.filter(or_(User.id == user.id, in_scope)).order_by(User.email)
    return q.filter(User.id == user.id)


def _validate_target_scope(
    user: User, db: Session, department_id: str, assigned_to: str, *, required: bool = True,
) -> None:
    """Valida a quién puede dirigir un encargado (o un empleado) una tarea/plantilla.

    - superadmin: sin restricción.
    - encargado (admin, o con acceso `manage`): puede difundir a cualquier
      departamento (`department_id`) sin restricción — es una bandeja compartida
      global, sin elegir persona. Si además señala a alguien puntual
      (`assigned_to`), debe caer dentro de su alcance de gestión — ver
      `_assignable_users_query`.
    - employee: no puede difundir a un departamento ni asignar a otra persona;
      sólo puede autoasignarse (o dejarla sin persona puntual).

    `required`: True exige que venga department_id o assigned_to (plantillas
    recurrentes, que no tienen sentido sin destinatario); False lo permite
    vacío (tarea suelta personal, sin depto ni asignado).
    """
    if user.role == ROLE_SUPERADMIN:
        return
    if required and not department_id and not assigned_to:
        raise HTTPException(400, "Elige un departamento o una persona")

    if is_manager(user):
        if assigned_to:
            allowed_users = {str(u.id) for u in _assignable_users_query(user, db).all()}
            if assigned_to not in allowed_users:
                raise HTTPException(403, "Solo puedes asignar a alguien dentro de tu alcance")
        return

    # employee (o cualquier rol no-encargado): sólo autoasignación
    if department_id:
        raise HTTPException(403, "No puedes asignar tareas a un departamento")
    if assigned_to and assigned_to != str(user.id):
        raise HTTPException(403, "Solo puedes asignarte tareas a ti mismo")


# Tope de fechas que genera un seguimiento con "Repetir" (diario por ~2 meses).
FOLLOW_UP_MAX_REPEATS = 60


def _follow_up_days(
    task: Task, first: date, frequency: str, day_of_week: str, day_of_month: str,
    days_of_week: list[str], repeat_until: str,
) -> list[date]:
    """Días de un seguimiento repetido: los que coinciden con la frecuencia
    (mismos criterios que las tareas recurrentes) desde `first` hasta
    `repeat_until`, que por defecto es la fecha límite de la tarea."""
    freq, dow, dom, custom_days = parse_recurrence(frequency, day_of_week, day_of_month, days_of_week)
    try:
        until = date.fromisoformat(repeat_until) if repeat_until else task.due_date
    except ValueError:
        raise HTTPException(422, "Fecha de fin de la repetición inválida")
    if until is None:
        raise HTTPException(422, "Indica hasta cuándo se repite (la tarea no tiene fecha límite)")
    if task.due_date and until > task.due_date:
        raise HTTPException(422, "La repetición debe terminar antes o el día de la fecha límite")
    if until < first:
        raise HTTPException(422, "La repetición debe terminar después del primer día")
    days = []
    d = first
    while d <= until:
        if matches_recurrence(freq, dow, dom, custom_days, d):
            days.append(d)
            if len(days) > FOLLOW_UP_MAX_REPEATS:
                raise HTTPException(422, f"La repetición genera más de {FOLLOW_UP_MAX_REPEATS} seguimientos; acorta el periodo")
        d += timedelta(days=1)
    if not days:
        raise HTTPException(422, "Ningún día del periodo coincide con la repetición")
    return days


def _parse_follow_up(task: Task, day: str, start: str, end: str, reminder_minutes: str, existing=None):
    """Valida un seguimiento (hora local) y devuelve (start_utc, end_utc, reminder).

    El día debe caer entre hoy y la fecha límite de la tarea; al editar se
    permite conservar un día que ya pasó."""
    try:
        d = date.fromisoformat(day)
        start_local = datetime.combine(d, datetime.strptime(start, "%H:%M").time())
        end_local = datetime.combine(d, datetime.strptime(end, "%H:%M").time())
    except ValueError:
        raise HTTPException(422, "Fecha u hora inválida")
    if end_local <= start_local:
        raise HTTPException(422, "La hora de fin debe ser posterior a la de inicio")
    if task.due_date and d > task.due_date:
        raise HTTPException(422, "El seguimiento debe ser antes o el día de la fecha límite")
    keeps_day = existing is not None and to_local(existing.start_at).date() == d
    if d < local_now().date() and not keeps_day:
        raise HTTPException(422, "El seguimiento no puede ser en un día que ya pasó")
    if reminder_minutes in ("", None):
        reminder = None
    else:
        try:
            reminder = int(reminder_minutes)
        except ValueError:
            raise HTTPException(422, "Recordatorio inválido")
        if reminder not in CALENDAR_REMINDER_OPTIONS:
            raise HTTPException(422, "Recordatorio inválido")
    return to_utc(start_local), to_utc(end_local), reminder


def _schedule_follow_up(f: TaskFollowUp, start_at: datetime, end_at: datetime, reminder: int | None) -> None:
    if (f.start_at, f.reminder_minutes) != (start_at, reminder):
        f.remind_at = compute_remind_at(start_at, False, reminder)
        f.reminded_at = None
    f.start_at, f.end_at, f.reminder_minutes = start_at, end_at, reminder


def task_view_condition(user: User):
    """Condición SQL de las tareas que `user` puede ver (igual que _can_view_task:
    su alcance con lectura de auditor, más las compartidas con él o con quien
    supervisa), o None si las ve todas. Sirve para combinar varias personas con
    AND (asistente MAX)."""
    if user.role in (ROLE_SUPERADMIN, ROLE_AUDITOR):
        return None
    conditions = [
        Task.created_by == user.id,
        Task.assigned_to == user.id,
        Task.id.in_(select(TaskShare.task_id).where(TaskShare.user_id == user.id)),
    ]
    if user.department_id:
        conditions.append(Task.department_id == user.department_id)
    granted = granted_department_ids(user)
    if granted:
        conditions.append(Task.department_id.in_(granted))
    overseen = shared_with_overseen_condition(user)
    if overseen is not None:
        conditions.append(overseen)
    return or_(*conditions)


# ── Creación ──────────────────────────────────────────────────────────────────

def create_task(
    db: Session,
    actor: User,
    *,
    title: str,
    description: str = "",
    priority: str = "medium",
    status: str = "pending",
    department_id: str = "",
    assigned_to: str = "",
    project_ids: list[str] | None = None,
    document_id: str = "",
    due_date: date | None = None,
    start_date: date | None = None,
    source_app: str | None = None,
    context: dict | None = None,
) -> Task:
    """Crea la tarea con su primer registro de historial, validando a quién la
    puede dirigir `actor` (HTTPException si no). Sin commit ni avisos."""
    _validate_target_scope(actor, db, department_id, assigned_to, required=False)
    # Antes de crear el objeto: al enlazarlo a sus proyectos ya queda en la sesión.
    check_start_due(start_date, due_date)

    task = Task(
        id=uuid.uuid4(),
        title=title.strip(),
        description=(description or "").strip() or None,
        priority=priority if priority in TASK_PRIORITIES else "medium",
        status=status if status in TASK_STATUSES else "pending",
        department_id=_effective_department_id(department_id, assigned_to, db),
        assigned_to=assigned_to if assigned_to else None,
        projects=_validated_projects(project_ids, actor, db),
        document_id=_validated_document_id(document_id, actor, db),
        created_by=actor.id,
        due_date=due_date,
        source_app=source_app,
        context=context,
    )
    # El inicio solo existe en tareas de proyectos con Gantt.
    if start_date and has_gantt(task):
        task.start_date = start_date
    db.add(task)
    db.flush()
    if task.status == TASK_DONE:
        task.completed_at = task.created_at
    db.add(TaskStatusHistory(
        id=uuid.uuid4(),
        task_id=task.id,
        from_status=None,
        to_status=task.status,
        changed_by=actor.id,
        changed_at=task.created_at,
    ))
    return task


def notify_task_created(task: Task, actor: User) -> None:
    """Avisos tras el commit: al asignado (si no es quien la creó) y al tablero."""
    if task.assigned_to and str(task.assigned_to) != str(actor.id):
        name = actor.name or actor.email
        realtime.notify_user(
            task.assigned_to,
            title=f"{name} te asignó una tarea",
            body=task.title,
            url=f"/tasks/{task.id}",
            kind="task_assigned",
        )
    realtime.notify_tasks()


def create_follow_ups(
    db: Session,
    actor: User,
    task: Task,
    *,
    day: str,
    start: str,
    end: str,
    reminder_minutes: str = "",
    note: str = "",
    repeat: bool = False,
    frequency: str = "",
    day_of_week: str = "",
    day_of_month: str = "",
    days_of_week: list[str] | None = None,
    repeat_until: str = "",
) -> dict:
    """Programa uno o varios seguimientos (con "Repetir") en `task`. Valida
    permisos, fechas y la repetición (HTTPException si algo no cuadra). Sin
    commit. Devuelve lo que necesitan los avisos y la auditoría."""
    if not _can_manage_follow_ups(actor, task, db):
        raise HTTPException(403, "No puedes programar seguimientos en esta tarea")
    if task.status == TASK_DONE or task.archived_at:
        raise HTTPException(422, "La tarea ya está terminada")
    start_at, end_at, reminder = _parse_follow_up(task, day, start, end, reminder_minutes)

    first = date.fromisoformat(day)
    if repeat:
        days = _follow_up_days(task, first, frequency, day_of_week, day_of_month, days_of_week or [], repeat_until)
        series_id = uuid.uuid4()
    else:
        days, series_id = [first], None
    note = (note or "").strip()[:500] or None
    created = []
    for d in days:
        # Zona fija (local_utc_offset_hours): basta con desplazar los días.
        shift = timedelta(days=(d - first).days)
        f = TaskFollowUp(task_id=task.id, note=note, series_id=series_id, created_by=actor.id)
        _schedule_follow_up(f, start_at + shift, end_at + shift, reminder)
        db.add(f)
        created.append(f)
    return {"follow_ups": created, "start_at": start_at, "end_at": end_at, "days": days, "series_id": series_id}


def notify_follow_ups_created(task: Task, actor: User, result: dict) -> None:
    """Avisa al asignado si el seguimiento lo programó alguien más."""
    start_at, days = result["start_at"], result["days"]
    when = f"{to_local(start_at):%d/%m %H:%M}"
    if len(days) > 1:
        when = f"{len(days)} seguimientos desde el {when}"
    if task.assigned_to and task.assigned_to != actor.id:
        realtime.notify_user(
            task.assigned_to,
            title=f"{actor.name or actor.email} programó un seguimiento",
            body=f"{task.title} · {when}",
            url=f"/tasks/{task.id}",
            kind="task_follow_up",
        )
    realtime.notify_task(task.id)


# ── Gantt: inicio y dependencias ──────────────────────────────────────────────
# Solo aplican a tareas que están en algún proyecto con Gantt activado. Si la
# tarea sale de esos proyectos, sus datos se conservan pero no se muestran.

def gantt_project_ids(task: Task) -> set:
    return {p.id for p in task.projects if p.gantt_enabled}


def has_gantt(task: Task) -> bool:
    return any(p.gantt_enabled for p in task.projects)


def check_start_due(start: date | None, due: date | None) -> None:
    if start and due and start > due:
        raise HTTPException(422, "La fecha de inicio no puede ser posterior a la fecha límite")


def is_blocked(task: Task) -> bool:
    """Bloqueada: no está lista y alguna tarea de la que depende tampoco."""
    return task.status != TASK_DONE and any(d.status != TASK_DONE for d in task.depends_on)


def dependency_candidates(task: Task, db: Session) -> list[Task]:
    """Tareas de los mismos proyectos con Gantt de las que `task` puede depender:
    sin ella misma, sin las que ya tiene y sin las que dependen directamente de
    ella (eso sería un ciclo inmediato)."""
    project_ids = gantt_project_ids(task)
    if not project_ids:
        return []
    exclude = {task.id} | {d.id for d in task.depends_on} | {b.id for b in task.blocks}
    rows = (
        db.query(Task)
        .filter(Task.projects.any(Project.id.in_(project_ids)), Task.archived_at.is_(None))
        .order_by(Task.title)
        .all()
    )
    return [t for t in rows if t.id not in exclude]


def _reaches(db: Session, start_id, target_id) -> bool:
    """¿Desde start_id, siguiendo "depende de", se llega a target_id?"""
    seen, frontier = {start_id}, [start_id]
    while frontier:
        rows = db.execute(
            select(task_dependencies.c.depends_on_id).where(task_dependencies.c.task_id.in_(frontier))
        ).scalars().all()
        frontier = []
        for nid in rows:
            if nid == target_id:
                return True
            if nid not in seen:
                seen.add(nid)
                frontier.append(nid)
    return False


def add_dependency(task: Task, depends_on_id: str, db: Session) -> Task:
    """`task` pasa a depender de `depends_on_id`. Ambas deben compartir un proyecto
    con Gantt y no puede formarse un ciclo. Sin commit."""
    try:
        dep_uuid = uuid.UUID(str(depends_on_id))
    except ValueError:
        raise HTTPException(422, "Tarea inválida")
    if dep_uuid == task.id:
        raise HTTPException(422, "Una tarea no puede depender de sí misma")
    other = db.query(Task).options(joinedload(Task.projects)).filter(Task.id == dep_uuid).first()
    if not other:
        raise HTTPException(404, "La tarea ya no existe")
    if not gantt_project_ids(task) & gantt_project_ids(other):
        raise HTTPException(422, "Solo puede depender de tareas del mismo proyecto con Gantt")
    if any(d.id == dep_uuid for d in task.depends_on):
        return other
    if _reaches(db, dep_uuid, task.id):
        raise HTTPException(422, f"«{other.title}» ya depende de esta tarea (directa o indirectamente); se formaría un ciclo")
    task.depends_on.append(other)
    return other


def remove_dependency(task: Task, depends_on_id: str) -> Task | None:
    for d in list(task.depends_on):
        if str(d.id) == str(depends_on_id):
            task.depends_on.remove(d)
            return d
    return None
