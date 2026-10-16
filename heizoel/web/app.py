"""FastAPI-App: Middleware, Statics, Router."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from heizoel import db
from heizoel.config import settings
from heizoel.web.auth import RedirectTo
from heizoel.web.routes import router
from heizoel.web.templating import BASE_DIR, templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Heizöl-Kaufberater", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=False,
    )
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    app.include_router(router)

    @app.exception_handler(RedirectTo)
    async def _redirect(request: Request, exc: RedirectTo):
        return RedirectResponse(exc.url, status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if exc.status_code in (403, 404):
            return templates.TemplateResponse(
                request, "error.html",
                {"user": None, "status": exc.status_code, "detail": exc.detail},
                status_code=exc.status_code,
            )
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)

    return app


app = create_app()
