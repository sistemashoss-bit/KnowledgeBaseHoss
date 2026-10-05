import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from html import escape

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
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
from app.auth.deps import require_csrf  # noqa: E402

app = FastAPI(
    title="Knowledge Base",
    version="1.0.0",
    lifespan=lifespan,
    dependencies=[Depends(require_csrf)],
)
app.mount("/assets", StaticFiles(directory=Path(__file__).resolve().parent / "app" / "assets"), name="assets")
app.state.limiter = limiter


@app.get("/health", include_in_schema=False)
def health():
    # Postgres es indispensable (503 si falla). Valkey es opcional: la app
    # funciona sin él, así que solo se reporta su estado.
    from sqlalchemy import text
    from app.config import settings
    from app.database import engine
    from app import valkey_client as vk

    checks = {}
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "down"

    if not settings.valkey_url:
        checks["valkey"] = "disabled"
    else:
        try:
            r = vk._get()
            checks["valkey"] = "ok" if r is not None and r.ping() else "down"
        except Exception:
            checks["valkey"] = "down"

    ok = checks["database"] == "ok"
    return JSONResponse(
        {"status": "ok" if ok else "error", "checks": checks},
        status_code=200 if ok else 503,
    )


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Servido en la raíz (no bajo /assets) para que su scope cubra todo el
    # sitio — necesario para que Web Push llegue sin importar qué página
    # esté abierta.
    path = Path(__file__).resolve().parent / "app" / "assets" / "sw.js"
    return FileResponse(path, media_type="application/javascript")
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(StarletteHTTPException)
async def file_too_large_handler(request: Request, exc: StarletteHTTPException):
    # 413 (archivo > 50 MB, ver storage.check_upload_sizes): mensaje legible en
    # vez del JSON crudo. htmx recibe texto plano (base.html lo muestra en alert).
    if exc.status_code != 413:
        return await http_exception_handler(request, exc)
    if request.headers.get("HX-Request"):
        return PlainTextResponse(exc.detail, status_code=413)
    return HTMLResponse(
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Archivo demasiado grande</title>'
        '<div style="font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px">'
        '<h1 style="font-size:1.25rem">Archivo demasiado grande</h1>'
        f'<p>{escape(exc.detail)}</p>'
        '<p><a href="javascript:history.back()">← Volver</a></p></div>',
        status_code=413,
    )

from app.auth.router import router as auth_router
from app.documents.router import router as documents_router
from app.departments.router import router as departments_router
from app.users.router import api_router as users_api_router, mgmt_router as users_mgmt_router
from app.chat.router import router as chat_router
from app.logs.router import router as logs_router
from app.zones.router import router as zones_router
from app.tasks.router import router as tasks_router, recurring_router, tags_router as task_tags_router
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
app.include_router(task_tags_router)  # idem: /tasks/tags antes del catch-all /tasks/{task_id}
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
    # Con sesión activa, el home redirige directo a documentos.
    if user:
        return RedirectResponse("/documents/", status_code=302)
    documents = rag.search_documents(q, build_access_filter(user)) if q else []
    return templates.TemplateResponse(
        request, "home.html",
        {"current_user": user, "query": q, "documents": documents},
    )
