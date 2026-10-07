"""Persistent appearance settings shared by the optional UI and headless controls."""

import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from ochecore.config import Settings, UIConfig
from ochecore.storage import write_private_json


class UISettings:
    def __init__(self, settings: Settings):
        self.path = settings.data_dir / "ui.json"
        self.lock = asyncio.Lock()
        self.error: str | None = None
        self.config = UIConfig()
        try:
            if self.path.exists():
                self.config = UIConfig.model_validate_json(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError):
            self.error = "Cannot load ui.json. Save UI settings to replace the invalid file."
        overrides = {
            key: value
            for key in UIConfig.model_fields
            if (value := getattr(settings, f"ui_{key}")) is not None
        }
        self.config = UIConfig.model_validate({**self.config.model_dump(), **overrides})

    def status(self) -> dict:
        return {**self.config.model_dump(), "error": self.error}

    async def configure(self, update: UIConfig) -> dict:
        async with self.lock:
            values = {**self.config.model_dump(), **update.model_dump(exclude_unset=True)}
            config = UIConfig.model_validate(values)
            write_private_json(self.path, config.model_dump())
            self.config, self.error = config, None
            return self.status()

    def allows_parent(self, origin: str | None) -> bool:
        return bool(
            self.config.embedded
            and self.config.parent_origin
            and origin == self.config.parent_origin
        )

    def frame_ancestors(self) -> str:
        if not self.config.embedded:
            return "'none'"
        return "'self'" + (f" {self.config.parent_origin}" if self.config.parent_origin else "")


def render_index(path: Path, config: UIConfig) -> HTMLResponse:
    source = path.read_text(encoding="utf-8")

    def attributes(match: re.Match) -> str:
        opening = re.sub(r'\sdata-(?:theme|embedded|ui-theme)="[^"]*"', "", match.group())
        return opening[:-1] + (
            f' data-theme="{config.theme}" data-ui-theme="{config.theme}"'
            f' data-embedded="{str(config.embedded).lower()}">'
        )

    return HTMLResponse(re.sub(r"<html\b[^>]*>", attributes, source, count=1, flags=re.IGNORECASE))


router = APIRouter()


@router.get("/api/ui")
async def ui_settings(request: Request):
    return request.app.state.ui.status()


@router.patch("/api/ui")
async def configure_ui(update: UIConfig, request: Request):
    if getattr(request.state, "ui_parent_request", False) and update.model_fields_set - {"theme"}:
        raise HTTPException(403, "The embedding parent can change only the UI theme.")
    return await request.app.state.ui.configure(update)
