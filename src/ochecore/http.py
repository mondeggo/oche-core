from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ochecore import __version__
from ochecore.config import ConnectionConfig


def same_origin(origin: str | None, host: str, scheme: str) -> bool:
    if not origin:
        return True  # CLI clients do not send Origin.
    parts = urlsplit(origin)
    return parts.scheme == scheme and parts.netloc == host


def create_router(static_dir: Path | None = None) -> APIRouter:
    router = APIRouter()

    if static_dir is not None:

        @router.get("/", include_in_schema=False)
        async def index():
            return FileResponse(static_dir / "index.html")

    @router.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__}

    @router.get("/readyz")
    async def ready(request: Request):
        runtime = request.app.state.runtime
        available = runtime.cloud.state == "connected"
        return JSONResponse({"ready": available}, status_code=200 if available else 503)

    @router.get("/api/status")
    async def status(request: Request):
        return request.app.state.runtime.status()

    @router.get("/api/config")
    async def config(request: Request):
        runtime = request.app.state.runtime
        return {
            **runtime.config.model_dump(),
            "api_base_url": runtime.settings.api_base_url,
            "locked_fields": [
                key for key in ConnectionConfig.model_fields if getattr(runtime.settings, key)
            ],
        }

    @router.get("/api/boards")
    async def boards(request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return {
                "boards": await runtime.cloud.list_boards(),
                "selected_board_id": runtime.config.board_id or None,
            }

    @router.put("/api/config")
    async def configure(config: ConnectionConfig, request: Request):
        runtime = request.app.state.runtime
        configured = runtime.settings.connection()
        for key in ConnectionConfig.model_fields:
            if getattr(runtime.settings, key) and getattr(config, key) != getattr(configured, key):
                raise HTTPException(
                    409, f"{key} is managed by .env or config/config.yaml. Edit that file."
                )
        await runtime.configure(config)
        return {"saved": True}

    @router.post("/api/auth/login")
    async def login(request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return await runtime.auth.start_login()

    @router.post("/api/auth/logout")
    async def logout(request: Request):
        await request.app.state.runtime.logout()
        return {"disconnected": True}

    @router.get("/api/events")
    async def events(request: Request):
        return list(request.app.state.runtime.bus.history)

    return router
