"""Jefe directo de una persona (PLAN_CATEGORIAS_TICKETS.md §3), para el primer paso
de las categorías de Comercial. Es el único lugar que conoce la jerarquía: hoy la
deduce del nivel del puesto (`User.position_level`, que manda hoss-api como
`Roles.level`) y de las sucursales; si algún día RH manda `reports_to`, solo cambia
esto.

Regla:
1. Candidatos: activos (no bot) del mismo departamento, con nivel mayor al de quien
   reporta.
2. En un departamento `branch_scoped` (Comercial), el candidato debe cubrir alguna
   sucursal de quien reporta: tenerla en sus UserBranch o tener `all_branches`.
3. Se queda el nivel más bajo de los candidatos (el inmediato superior); pueden ser
   varios (dos supervisores en la misma sucursal).
4. Si no hay candidatos: los admins del departamento con la misma condición de
   sucursal, salvo los que tienen nivel igual o menor al de quien reporta.
5. Si tampoco hay: lista vacía (quien reporta ya es el de más arriba; el paso se
   salta).

Empate (varios jefes): `pick_direct_boss` elige al que tenga menos tickets activos
asignados y, si empatan, por nombre.
"""
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models import ROLE_ADMIN, TICKET_ACTIVE_STATUSES, Department, Ticket, User


@dataclass(frozen=True)
class Person:
    """Lo que la regla necesita de una persona (sin sesión de base, para probarla)."""
    id: object
    name: str
    level: int | None
    is_admin: bool
    all_branches: bool
    branch_ids: frozenset


def _person(u: User) -> Person:
    return Person(
        id=u.id,
        name=(u.name or u.email or "").lower(),
        level=u.position_level,
        is_admin=u.role == ROLE_ADMIN,
        all_branches=bool(u.all_branches),
        branch_ids=frozenset(ub.branch_id for ub in u.user_branches),
    )


def requester_branch_ids(u: User) -> frozenset:
    """Sucursales de quien reporta: las de hoss (UserBranch) o, si no tiene, la local."""
    ids = frozenset(ub.branch_id for ub in u.user_branches)
    if not ids and u.branch_id:
        ids = frozenset([u.branch_id])
    return ids


def select_bosses(requester: Person, requester_branches: frozenset, branch_scoped: bool, others: list[Person]) -> list[Person]:
    """La regla pura: de los demás miembros activos del departamento, los jefes directos."""
    def covers(p: Person) -> bool:
        return not branch_scoped or p.all_branches or bool(p.branch_ids & requester_branches)

    pool = [p for p in others if p.id != requester.id and covers(p)]
    if requester.level is not None:
        above = [p for p in pool if p.level is not None and p.level > requester.level]
        if above:
            nearest = min(p.level for p in above)
            return [p for p in above if p.level == nearest]
    return [
        p for p in pool
        if p.is_admin and (requester.level is None or p.level is None or p.level > requester.level)
    ]


def direct_bosses(user: User, db: Session) -> list[User]:
    """Jefes directos de `user` (vacío si no tiene departamento o ya es el de más arriba)."""
    if not user.department_id:
        return []
    dept = db.get(Department, user.department_id)
    members = (
        db.query(User)
        .options(selectinload(User.user_branches))
        .filter(
            User.department_id == user.department_id, User.id != user.id,
            User.is_active == True, User.is_bot == False,  # noqa: E712
        )
        .all()
    )
    by_id = {u.id: u for u in members}
    chosen = select_bosses(
        _person(user), requester_branch_ids(user), bool(dept and dept.branch_scoped), [_person(u) for u in members],
    )
    return [by_id[p.id] for p in chosen]


def pick_direct_boss(user: User, db: Session) -> User | None:
    """El jefe al que se asigna el ticket: si son varios, el de menos tickets activos
    asignados (empate: por nombre)."""
    bosses = direct_bosses(user, db)
    if len(bosses) <= 1:
        return bosses[0] if bosses else None
    counts = dict(
        db.query(Ticket.assigned_to, func.count(Ticket.id))
        .filter(Ticket.assigned_to.in_([b.id for b in bosses]), Ticket.status.in_(TICKET_ACTIVE_STATUSES))
        .group_by(Ticket.assigned_to)
        .all()
    )
    return min(bosses, key=lambda b: (counts.get(b.id, 0), (b.name or b.email or "").lower()))
