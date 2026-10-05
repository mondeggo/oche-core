from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ochecore import __version__
from ochecore.api.http import create_router as create_http_router
from ochecore.api.http import same_origin
from ochecore.api.websocket import router as websocket_router
from ochecore.autodarts.auth import safe_error
from ochecore.autodarts.errors import ConnectionProblem
from ochecore.config import Settings
from ochecore.runtime import Runtime

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with httpx.AsyncClient(
            timeout=settings.request_timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        ) as http:
            runtime = Runtime(settings, http)
            application.state.runtime = runtime
            runtime.start()
            try:
                yield
            finally:
                await runtime.close()

    application = FastAPI(
        title="OcheCore",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.ui_enabled else None,
        redoc_url=None,
    )
    if settings.ui_enabled:
        application.mount("/static", StaticFiles(directory=STATIC), name="static")

    application.include_router(create_http_router(STATIC if settings.ui_enabled else None))
    application.include_router(websocket_router)

    @application.middleware("http")
    async def browser_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(
            request.headers.get("origin"), request.headers.get("host", ""), request.url.scheme
        ):
            return JSONResponse({"detail": "Origin not allowed."}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH"} and not request.headers.get(
            "content-type", ""
        ).startswith("application/json"):
            return JSONResponse({"detail": "Use application/json."}, status_code=415)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        policy = "default-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        if request.url.path in {"/docs", "/redoc", "/docs/oauth2-redirect"}:
            policy += (
                "; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
                "; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
                "; img-src 'self' data: https://fastapi.tiangolo.com"
            )
        response.headers["Content-Security-Policy"] = policy
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @application.exception_handler(ConnectionProblem)
    async def connection_error(request: Request, exc: ConnectionProblem):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @application.exception_handler(httpx.HTTPError)
    @application.exception_handler(OSError)
    async def external_error(request: Request, exc: Exception):
        return JSONResponse({"detail": safe_error(exc)}, status_code=502)

    return application


def main() -> None:
    from ochecore.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
