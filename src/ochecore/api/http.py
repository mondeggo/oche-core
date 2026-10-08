import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from ochecore import __version__
from ochecore.api.ui import render_index
from ochecore.config import ConnectionConfig
from ochecore.events import DebugRecording, Event
from ochecore.integrations.caller.service import CallerConfig, CallerTest
from ochecore.integrations.wled.service import (
    Device,
    DeviceAddress,
    DraftPreview,
    Power,
    Preview,
    ProfileName,
    ProfileSelection,
    WLEDConfig,
)


class DeviceEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def configuration_revision(runtime, request: Request) -> str:
    path = request.url.path
    if path.startswith("/api/wled"):
        data = runtime.wled.config.model_dump(mode="json")
    elif path.startswith("/api/caller"):
        data = {
            "active_profile": runtime.wled.config.active_profile,
            "config": runtime.caller.config.model_dump(mode="json"),
        }
    else:
        return runtime.profiles.revision
    content = json.dumps(data, sort_keys=True).encode()
    return f'"{hashlib.sha256(content).hexdigest()}"'


def check_revision(request: Request, runtime) -> None:
    expected = request.headers.get("if-match")
    if (
        expected
        and expected != "*"
        and configuration_revision(runtime, request)
        not in {value.strip() for value in expected.split(",")}
    ):
        raise HTTPException(
            412, "Profile settings changed in another client. Reload and try again."
        )


def same_origin(origin: str | None, host: str, scheme: str) -> bool:
    if not origin:
        return True  # CLI clients do not send Origin.
    parts = urlsplit(origin)
    return parts.scheme == scheme and parts.netloc == host


def create_router(static_dir: Path | None = None) -> APIRouter:
    router = APIRouter()

    if static_dir is not None:

        @router.get("/", include_in_schema=False)
        async def index(request: Request):
            return render_index(static_dir / "index.html", request.app.state.ui.config)

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

    @router.get("/api/game")
    async def game(request: Request):
        return request.app.state.runtime.game_state()

    @router.get("/api/wled")
    async def wled_config(request: Request, response: Response) -> WLEDConfig:
        runtime = request.app.state.runtime
        async with runtime.lock:
            response.headers["ETag"] = configuration_revision(runtime, request)
            return runtime.wled.config

    @router.put("/api/wled")
    async def configure_wled(config: WLEDConfig, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            await runtime.profiles.configure_wled(config)
            response.headers["ETag"] = configuration_revision(runtime, request)
        return {"saved": True}

    @router.patch("/api/wled")
    async def update_wled(config: WLEDConfig, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            if config.model_fields_set:
                updated = runtime.wled.config.model_copy(
                    update={key: getattr(config, key) for key in config.model_fields_set}
                )
                try:
                    updated = WLEDConfig.model_validate(updated.model_dump())
                except ValidationError as exc:
                    raise HTTPException(422, exc.errors()[0]["msg"]) from exc
                await runtime.profiles.configure_wled(updated)
            response.headers["ETag"] = configuration_revision(runtime, request)
        return {"saved": True}

    @router.post("/api/wled/devices", status_code=201)
    async def add_wled_device(device: Device, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            config = runtime.wled.config.model_dump(mode="json")
            config["devices"].append(device.model_dump(mode="json"))
            try:
                updated = WLEDConfig.model_validate(config)
            except ValidationError as exc:
                raise HTTPException(422, exc.errors()[0]["msg"]) from exc
            await runtime.profiles.configure_wled(updated)
            response.headers["ETag"] = configuration_revision(runtime, request)
        return {"saved": True, "device": device.model_dump(mode="json")}

    @router.get("/api/wled/status")
    async def wled_status(request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            response.headers["ETag"] = configuration_revision(runtime, request)
            return runtime.wled.status()

    @router.get("/api/profiles")
    async def profiles(request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            response.headers["ETag"] = configuration_revision(runtime, request)
            return runtime.profiles.summary()

    @router.post("/api/profiles")
    @router.post("/api/wled/profiles")
    async def wled_create_profile(profile: ProfileName, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            result = await runtime.profiles.create(profile.name, profile.source)
            response.headers["ETag"] = configuration_revision(runtime, request)
            return result

    @router.put("/api/profile")
    @router.put("/api/wled/profile")
    async def wled_select_profile(profile: ProfileSelection, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            result = await runtime.profiles.select(profile.id)
            response.headers["ETag"] = configuration_revision(runtime, request)
            return result

    @router.delete("/api/profiles/{profile_id}")
    @router.delete("/api/wled/profiles/{profile_id}")
    async def wled_delete_profile(profile_id: str, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            result = await runtime.profiles.delete(profile_id)
            response.headers["ETag"] = configuration_revision(runtime, request)
            return result

    @router.patch("/api/wled/devices/{device_id}")
    async def update_wled_device(
        device_id: str, update: DeviceEnabled, request: Request, response: Response
    ):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            config = runtime.wled.config.model_copy(deep=True)
            device = next((item for item in config.devices if item.id == device_id), None)
            if device is None:
                raise HTTPException(404, "Unknown WLED device. Save it first.")
            device.enabled = update.enabled
            await runtime.profiles.configure_wled(config)
            response.headers["ETag"] = configuration_revision(runtime, request)
        return {"saved": True}

    @router.post("/api/wled/{device_id}/probe")
    async def wled_probe(device_id: str, request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return await runtime.wled.probe(device_id)

    @router.post("/api/wled/probe")
    async def wled_probe_address(address: DeviceAddress, request: Request):
        return await request.app.state.runtime.wled.probe_address(address)

    @router.post("/api/wled/preview")
    async def wled_preview(draft: DraftPreview, request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return await runtime.wled.preview_draft(draft)

    @router.post("/api/wled/{device_id}/outputs/{output_id}/power")
    async def wled_output_power(device_id: str, output_id: int, power: Power, request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return await runtime.wled.output_power(device_id, output_id, power.on)

    @router.post("/api/wled/power")
    @router.post("/api/wled/{device_id}/power")
    async def wled_power(power: Power, request: Request, device_id: str | None = None):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return await runtime.wled.power(power.on, device_id)

    @router.post("/api/wled/discover")
    async def wled_discover(request: Request):
        return await request.app.state.runtime.wled.discover()

    @router.post("/api/wled/{device_id}/test")
    async def wled_test(device_id: str, preview: Preview, request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return await runtime.wled.test(device_id, preview)

    @router.get("/api/boards")
    async def boards(request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return {
                "boards": await runtime.cloud.list_boards(),
                "selected_board_id": runtime.config.board_id or None,
            }

    @router.get("/api/caller")
    async def caller_config(request: Request, response: Response) -> CallerConfig:
        runtime = request.app.state.runtime
        async with runtime.lock:
            response.headers["ETag"] = configuration_revision(runtime, request)
            return runtime.caller.config

    @router.put("/api/caller")
    async def configure_caller(config: CallerConfig, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            await runtime.profiles.configure_caller(config)
            response.headers["ETag"] = configuration_revision(runtime, request)
        return {"saved": True}

    @router.patch("/api/caller")
    async def update_caller(config: CallerConfig, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            if config.model_fields_set:
                updated = runtime.caller.config.model_copy(
                    update={key: getattr(config, key) for key in config.model_fields_set}
                )
                await runtime.profiles.configure_caller(updated)
            response.headers["ETag"] = configuration_revision(runtime, request)
        return {"saved": True}

    @router.get("/api/caller/status")
    async def caller_status(request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            response.headers["ETag"] = configuration_revision(runtime, request)
            return runtime.caller.status()

    @router.get("/api/caller/voices")
    async def caller_voices(request: Request):
        return request.app.state.runtime.caller.library.catalogue()

    @router.post("/api/caller/voices/{voice_id}/install", status_code=202)
    async def caller_install(voice_id: str, request: Request, response: Response):
        runtime = request.app.state.runtime
        async with runtime.lock:
            check_revision(request, runtime)
            await runtime.profiles.configure_caller(
                runtime.caller.config.model_copy(update={"voice": voice_id})
            )
            response.headers["ETag"] = configuration_revision(runtime, request)
        if runtime.caller.library.installed(voice_id):
            return {"state": "installed", "voice_id": voice_id}
        return runtime.caller.status()["download"]

    @router.post("/api/caller/test")
    async def caller_test(sample: CallerTest, request: Request):
        return request.app.state.runtime.caller.test(sample)

    @router.post("/api/caller/stop")
    async def caller_stop(request: Request):
        request.app.state.runtime.caller.stop()
        return {"stopped": True}

    @router.get("/api/caller/audio/{voice_id}/{clip}")
    async def caller_audio(voice_id: str, clip: str, request: Request):
        library = request.app.state.runtime.caller.library
        return FileResponse(library.clip_path(voice_id, clip))

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
    async def events(request: Request) -> list[Event]:
        return list(request.app.state.runtime.bus.normalized_history)

    @router.get("/api/events/raw")
    async def raw_events(request: Request):
        return list(request.app.state.runtime.bus.raw_history)

    @router.get("/api/events/debug")
    async def debug_status(request: Request):
        return request.app.state.runtime.debug.status()

    @router.put("/api/events/debug")
    async def debug_recording(config: DebugRecording, request: Request):
        runtime = request.app.state.runtime
        async with runtime.lock:
            return await runtime.debug.configure(config.enabled)

    @router.get("/api/events/debug/file")
    async def debug_file(request: Request):
        path = request.app.state.runtime.debug.download_path()
        return FileResponse(path, media_type="application/x-ndjson", filename=path.name)

    return router
