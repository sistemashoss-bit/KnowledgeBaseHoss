"""Routers de tareas. Las rutas viven en app/tasks/routes/; las de /tasks se
incluyen en el mismo orden en que estaban en este archivo (/{task_id} captura
cualquier segmento: las literales como /archived y /stream van antes).
main.py incluye recurring_router y tags_router antes que router por lo mismo."""
from fastapi import APIRouter

from app.tasks.routes import views, evidences, edit, collab, follow_ups
from app.tasks.routes.recurring import recurring_router
from app.tasks.routes.tags import tags_router

__all__ = ["router", "recurring_router", "tags_router"]

router = APIRouter(prefix="/tasks", tags=["tasks"])
for _module in (views, evidences, edit, collab, follow_ups):
    router.include_router(_module.router)
