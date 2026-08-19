import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.database import engine
    from app.models import Base
    from app import search as search_module
    from app.messaging.cleanup import scheduler_loop

    Base.metadata.create_all(bind=engine)
    search_module.ensure_indices()

    # Catch-up: si el server reinició pasada la hora del scheduler, genera hoy.
    # Idempotente gracias a RecurringTask.last_generated_on.
    try:
        from app.tasks.recurring import generate_due_tasks
        await asyncio.to_thread(generate_due_tasks)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("startup recurring generation failed")

    cleanup_task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Knowledge Base", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

from app.auth.router import router as auth_router
from app.documents.router import router as documents_router
from app.departments.router import router as departments_router
from app.users.router import api_router as users_api_router, mgmt_router as users_mgmt_router
from app.chat.router import router as chat_router
from app.logs.router import router as logs_router
from app.zones.router import router as zones_router
from app.tasks.router import router as tasks_router, recurring_router
from app.projects.router import router as projects_router
from app.messaging.router import router as messaging_router
from app.reports.router import router as reports_router
from app.notifications.router import router as notifications_router
from app.auth.deps import get_current_user
from app.database import get_db
from app.permissions import build_access_filter
from app import rag
from app.templating import templates

app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(departments_router)
app.include_router(users_api_router)
app.include_router(users_mgmt_router)
app.include_router(chat_router)
app.include_router(logs_router)
app.include_router(zones_router)
app.include_router(recurring_router)  # antes de tasks_router: evita el catch-all /tasks/{task_id}
app.include_router(tasks_router)
app.include_router(projects_router)
app.include_router(messaging_router)
app.include_router(reports_router)
app.include_router(notifications_router)


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
def home(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    documents = rag.search_documents(q, build_access_filter(user)) if q else []
    return templates.TemplateResponse(
        request, "home.html",
        {"current_user": user, "query": q, "documents": documents},
    )
