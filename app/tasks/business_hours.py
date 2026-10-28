"""Tiempo transcurrido contando solo el horario laboral.

Las fechas de la base son UTC naive; se pasan a la hora local del negocio
(settings.local_utc_offset_hours) y de cada día solo cuenta su tramo de
settings.work_hours. Así una tarea que entra a "En progreso" un lunes a las
18:00 y sale el martes a las 9:00 suma 2 h, no 15.
"""
from datetime import datetime, timedelta

from app.config import settings


def business_seconds(start: datetime, end: datetime) -> float:
    if end <= start:
        return 0.0
    offset = timedelta(hours=settings.local_utc_offset_hours)
    start, end = start + offset, end + offset
    total = 0.0
    day = datetime(start.year, start.month, start.day)
    while day < end:
        hours = settings.work_hours.get(day.weekday())
        if hours:
            lo = max(start, day + timedelta(hours=hours[0]))
            hi = min(end, day + timedelta(hours=hours[1]))
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


_DAY_NAMES = ["L", "M", "X", "J", "V", "S", "D"]


def business_hours_label() -> str:
    """'L–V 8:00–19:00 · S 8:00–15:00': días seguidos con el mismo horario juntos."""
    groups: list[list] = []
    for d in range(7):
        hours = settings.work_hours.get(d)
        if hours and groups and groups[-1][2] == hours and groups[-1][1] == d - 1:
            groups[-1][1] = d
        elif hours:
            groups.append([d, d, hours])
    return " · ".join(
        (_DAY_NAMES[a] if a == b else f"{_DAY_NAMES[a]}–{_DAY_NAMES[b]}") + f" {h[0]}:00–{h[1]}:00"
        for a, b, h in groups
    )
