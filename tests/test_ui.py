import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ochecore.cli import execute, parser, run
from ochecore.config import Settings, UIConfig
from ochecore.main import create_app


def test_ui_settings_persist_and_render_before_browser_scripts(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/ui").json() == {
            "embedded": False,
            "theme": "dark",
            "parent_origin": "",
            "error": None,
        }
        assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]
        saved = client.patch(
            "/api/ui",
            json={
                "embedded": True,
                "theme": "light",
                "parent_origin": "http://oche.local:3000/",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["parent_origin"] == "http://oche.local:3000"
        page = client.get("/")
        assert 'data-theme="light"' in page.text
        assert 'data-ui-theme="light"' in page.text
        assert 'data-embedded="true"' in page.text
        assert (
            "frame-ancestors 'self' http://oche.local:3000;"
            in page.headers["content-security-policy"]
        )
        assert client.get("/api/ui").headers["cache-control"] == "no-store"
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/ui").json() == saved.json()
        client.patch("/api/ui", json={"embedded": False})
        assert client.get("/api/ui").json()["theme"] == "light"
        assert "frame-ancestors 'none'" in client.get("/").headers["content-security-policy"]


def test_ui_controls_work_headless_and_same_origin_embedding_is_explicit(settings):
    settings.ui_enabled = False
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 404
        assert client.patch("/api/ui", json={"embedded": True}).status_code == 200
        assert "frame-ancestors 'self';" in client.get("/api/ui").headers["content-security-policy"]
        assert client.get("/api/ui").json()["embedded"] is True


def test_embedding_parent_may_change_only_theme_and_other_origins_stay_blocked(settings):
    settings.ui_embedded = True
    settings.ui_parent_origin = "https://oche.example"
    with TestClient(create_app(settings)) as client:
        origin = {"Origin": "https://oche.example"}
        options = client.options(
            "/api/ui",
            headers={
                **origin,
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert options.status_code == 204
        assert options.headers["access-control-allow-origin"] == "https://oche.example"
        assert options.headers["access-control-allow-methods"] == "GET, PATCH"
        assert "origin" in options.headers["vary"].lower()
        updated = client.patch("/api/ui", json={"theme": "light"}, headers=origin)
        assert updated.status_code == 200
        assert updated.headers["access-control-allow-origin"] == "https://oche.example"
        assert updated.json()["theme"] == "light"
        for body in ({"embedded": False}, {"parent_origin": "https://another.example"}):
            assert client.patch("/api/ui", json=body, headers=origin).status_code == 403
        assert client.post("/api/auth/logout", json={}, headers=origin).status_code == 403
        assert (
            "access-control-allow-origin" not in client.get("/api/status", headers=origin).headers
        )
        for stranger in ("https://other.example", "https://oche.example.attacker.test", "null"):
            headers = {"Origin": stranger}
            assert (
                client.patch("/api/ui", json={"theme": "dark"}, headers=headers).status_code == 403
            )
            assert (
                "access-control-allow-origin" not in client.get("/api/ui", headers=headers).headers
            )
        assert client.get("/api/ui").json()["theme"] == "light"


def test_parent_control_is_disabled_when_embedding_is_off(settings):
    settings.ui_parent_origin = "https://oche.example"
    with TestClient(create_app(settings)) as client:
        response = client.patch(
            "/api/ui", json={"theme": "light"}, headers={"Origin": "https://oche.example"}
        )
        assert response.status_code == 403
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "origin",
    [
        "https://*.example",
        "https://host/path",
        "https://user:pass@host",
        "https://host?query=1",
        "https://host#fragment",
        "https://host;frame-ancestors*",
        "http://host:70000",
        "https://host\n",
        "file:///tmp/index",
        "https://host\\evil",
        "https://host\x00evil",
    ],
)
def test_parent_origin_accepts_only_exact_http_origins(origin):
    with pytest.raises(ValidationError):
        UIConfig(parent_origin=origin)


def test_parent_origin_normalization_and_optional_startup_overrides(settings, monkeypatch):
    assert (
        UIConfig(parent_origin="HTTPS://OCHE.EXAMPLE:443/").parent_origin == "https://oche.example"
    )
    assert UIConfig(parent_origin="http://[::1]:3000/").parent_origin == "http://[::1]:3000"
    with TestClient(create_app(settings)) as client:
        client.patch("/api/ui", json={"embedded": True, "theme": "light"})
    monkeypatch.setenv("OCHECORE_UI_THEME", "dark")
    overridden = Settings(_env_file=None, data_dir=settings.data_dir, client_id="", board_id="")
    with TestClient(create_app(overridden)) as client:
        assert client.get("/api/ui").json()["theme"] == "dark"
        assert client.get("/api/ui").json()["embedded"] is True
        assert client.patch("/api/ui", json={"theme": "light"}).json()["theme"] == "light"
    with TestClient(create_app(overridden)) as client:
        assert client.get("/api/ui").json()["theme"] == "dark"


def test_invalid_ui_file_can_be_repaired_without_blocking_core(settings):
    settings.data_dir.mkdir(exist_ok=True)
    (settings.data_dir / "ui.json").write_text('{"theme":"invalid"}', encoding="utf-8")
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200
        assert "Cannot load" in client.get("/api/ui").json()["error"]
        assert client.patch("/api/ui", json={"theme": "light"}).json()["error"] is None
        assert client.patch("/api/ui", json={"theme": "unknown"}).status_code == 422
        assert client.patch("/api/ui", json={"unknown": True}).status_code == 422
    assert json.loads((settings.data_dir / "ui.json").read_text())["theme"] == "light"


def test_cli_ui_updates_supplied_fields_and_serves_with_startup_flags(
    settings, capsys, monkeypatch
):
    with TestClient(create_app(settings)) as client:
        execute(parser().parse_args(["ui", "--embedded", "--theme", "light", "--json"]), client)
        assert json.loads(capsys.readouterr().out)["embedded"] is True
        execute(parser().parse_args(["ui", "--no-embedded"]), client)
        output = capsys.readouterr().out
        assert "Embedded: no" in output
        assert "Theme: light" in output
    monkeypatch.setattr("ochecore.cli.Settings", lambda: settings)
    apps = []
    monkeypatch.setattr("ochecore.cli.uvicorn.run", lambda app, **kwargs: apps.append(app))
    assert (
        run(["serve", "--embedded", "--theme", "dark", "--parent-origin", "http://oche.local:3000"])
        == 0
    )
    assert apps[0].state.ui.config.embedded is True
    assert apps[0].state.ui.config.theme == "dark"
    assert apps[0].state.ui.config.parent_origin == "http://oche.local:3000"


@pytest.mark.parametrize(
    "arguments,method,path,body",
    [
        (
            ["profiles", "create", "Quiet", "--source", "blank"],
            "POST",
            "/api/profiles",
            {"name": "Quiet", "source": "blank"},
        ),
        (["profiles", "use", "default"], "PUT", "/api/profile", {"id": "default"}),
        (["profiles", "delete", "quiet"], "DELETE", "/api/profiles/quiet-id", {}),
        (
            ["wled", "profiles", "create", "Quiet", "--blank"],
            "POST",
            "/api/profiles",
            {"name": "Quiet", "source": "blank"},
        ),
    ],
)
def test_cli_application_profiles_use_common_api_and_revision(
    arguments, method, path, body, capsys
):
    calls = []
    data = {
        "active_profile": "default",
        "profiles": [
            {"id": "default", "name": "Default"},
            {"id": "quiet-id", "name": "Quiet"},
        ],
    }

    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/api/profiles"
            return httpx.Response(200, json=data, headers={"ETag": '"current"'})
        calls.append(
            (
                request.method,
                request.url.path,
                json.loads(request.content),
                request.headers.get("if-match"),
            )
        )
        return httpx.Response(200, json=data)

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as client:
        execute(parser().parse_args(arguments), client)
    assert calls == [(method, path, body, '"current"')]
    assert json.loads(capsys.readouterr().out) == data
