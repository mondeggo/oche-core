"""Smoke-test an installed distribution without an account, hardware or network access."""

import subprocess
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from fastapi.testclient import TestClient

from ochecore import __version__
from ochecore.config import Settings
from ochecore.integrations.caller.voices import VOICES
from ochecore.main import create_app


def reject_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected network request: {request.method} {request.url.path}")


subprocess.run(["ochecore", "--version"], check=True)
assert version("ochecore") == __version__, "Package and runtime versions must match."
assert VOICES, "The voice catalogue must be included in the package."

for ui_enabled in (True, False):
    with TemporaryDirectory(prefix="ochecore-build-") as directory:
        settings = Settings(
            _env_file=None,
            data_dir=Path(directory),
            client_id="",
            board_id="",
            ui_enabled=ui_enabled,
        )
        app = create_app(settings, transport=httpx.MockTransport(reject_request))
        with TestClient(app) as client:
            assert client.get("/healthz").json() == {"status": "ok", "version": __version__}
            assert client.get("/api/status").status_code == 200
            assert client.get("/api/profiles").json()["active_profile"] == "default"
            assert client.get("/api/ui").status_code == 200
            for path in (
                "/",
                "/static/app.js",
                "/static/theme.js",
                "/static/client.mjs",
                "/static/select.js",
                "/static/wled.js",
                "/static/style.css",
                "/static/caller.js",
            ):
                assert client.get(path).status_code == (200 if ui_enabled else 404), path
            if ui_enabled:
                assert "javascript" in client.get("/static/client.mjs").headers["content-type"]

print("Installed CLI, voice catalogue, API and optional UI passed.")
