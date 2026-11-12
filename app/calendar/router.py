"""Calendario personal: citas y actividades de cada usuario, más los calendarios
ajenos que tiene agregados (admin → su gente; superadmin → cualquiera) y,
opcionalmente, las fechas límite de tareas y proyectos.

Los seguimientos de tareas (TaskFollowUp) salen siempre, como bloques con hora,
en el calendario de su responsable (el asignado, o el creador si no hay).

El dueño puede compartir un evento con otras personas: les aparece en su
calendario (solo lectura), lo comentan y reciben sus avisos y recordatorios.

Los eventos pueden repetirse con los mismos criterios que las tareas
recurrentes (diario, semanal, mensual, días específicos); las ocurrencias se
calculan al consultar y la serie se edita completa. Los eventos aceptan
comentarios del dueño y de quien puede ver su calendario.

Fechas: se guardan en UTC; hacia y desde el navegador viajan en hora local del
negocio sin offset (FullCalendar corre con timeZone 'UTC' para mostrarlas tal
cual, igual que el resto de la app muestra todo fijo en hora de Monterrey).

Las rutas viven en app/calendar/routes/ y se incluyen en el mismo orden en que
estaban en este archivo; lo compartido está en app/calendar/helpers.py.
"""
from fastapi import APIRouter

from app.calendar.routes import views, events, meetings, comments, attachments, subscriptions

router = APIRouter(prefix="/calendar", tags=["calendar"])
for _module in (views, events, meetings, comments, attachments, subscriptions):
    router.include_router(_module.router)
