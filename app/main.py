"""The FastAPI app: routers, error handling, and the publish scheduler.

Run it:  .venv/Scripts/python -m alembic upgrade head      (once per deploy)
         .venv/Scripts/python -m uvicorn app.main:app --reload --port 8787
Docs:    http://localhost:8787/docs  (hidden when APP_ENV=production)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import runtime
from .api import admin, ai, auth, channels, compliance, content
from .database import SessionLocal, schema_problem
from .seed import seed
from .services import live_watch, workflow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("marketing_ops")


def scheduler_tick() -> None:
    """Publish whatever is due. Runs in a worker thread so it never blocks requests."""
    with SessionLocal() as db:
        try:
            ran = workflow.run_due_jobs(db, runtime.settings)
            db.commit()
            if ran:
                log.info("Scheduler ran %d publish job(s)", ran)
            # Then re-read recently published posts to confirm they still say what was approved.
            checked = live_watch.run_due_checks(db, runtime.settings)
            db.commit()
            if checked:
                log.info("Live watch checked %d published item(s)", checked)
        except Exception:
            db.rollback()
            log.exception("Scheduler tick failed")


async def scheduler_loop() -> None:
    while True:
        await asyncio.to_thread(scheduler_tick)
        await asyncio.sleep(runtime.settings.scheduler_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The app never creates or alters tables: Alembic owns the schema. If a deploy
    # forgot to migrate, stop here with the exact command rather than half-work.
    problem = schema_problem()
    if problem:
        log.error(problem)
        raise RuntimeError(problem)
    with SessionLocal() as db:
        seed(db, runtime.settings)
    task = asyncio.create_task(scheduler_loop()) if runtime.settings.scheduler_enabled else None
    yield
    if task:
        task.cancel()


PRODUCTION = runtime.settings.app_env == "production"
app = FastAPI(
    title="Marketing Operations Console",
    version="0.1.0",
    lifespan=lifespan,
    # Interactive API docs are for development; in production they map your API for attackers.
    docs_url=None if PRODUCTION else "/docs",
    redoc_url=None,
    openapi_url=None if PRODUCTION else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=runtime.settings.cors_origins,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Cheap, standard hardening on every API response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"   # API data is personal and changes constantly
    if PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # Log the details for us; show the user nothing internal.
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on our side. It has been logged."})


@app.exception_handler(workflow.WorkflowError)
async def workflow_error(_: Request, exc: workflow.WorkflowError):
    # The compliance report rides along, so the editor can highlight what blocked it.
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc), "report": exc.report})


for module in (auth, content, compliance, ai, admin, channels):
    app.include_router(module.router)
