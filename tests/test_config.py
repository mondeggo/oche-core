import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ochecore.config import Settings
from ochecore.main import create_app


@pytest.mark.parametrize("saved_id", [None, "", "   ", "saved-client"])
def test_client_fallback_without_yaml(tmp_path, monkeypatch, saved_id):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OCHECORE_CLIENT_ID", raising=False)
    if saved_id is not None:
        (tmp_path / "connection.json").write_text(
            json.dumps({"client_id": saved_id}), encoding="utf-8"
        )
    settings = Settings(_env_file=None, data_dir=tmp_path)
    expected = "saved-client" if saved_id == "saved-client" else "darts-caller"
    assert settings.connection().client_id == expected
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/config").json()["locked_fields"] == []
        assert client.put("/api/config", json={"client_id": "custom"}).status_code == 200
        assert client.get("/api/config").json()["client_id"] == "custom"
        assert client.put("/api/config", json={"client_id": ""}).status_code == 200
        assert client.get("/api/config").json()["client_id"] == "darts-caller"


def test_yaml_defaults_and_environment_overrides(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text('client_id: "yaml-client"\nport: 8091\n', encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "yaml_file", config_file)
    settings = Settings(_env_file=None, data_dir=tmp_path)
    assert settings.client_id == "yaml-client"
    assert settings.port == 8091
    with TestClient(create_app(settings)) as client:
        assert "client_id" in client.get("/api/config").json()["locked_fields"]
        assert client.put("/api/config", json={"client_id": "changed"}).status_code == 409
    monkeypatch.setenv("OCHECORE_CLIENT_ID", "environment-client")
    assert Settings(_env_file=None).client_id == "environment-client"
    # Empty Compose variables must not erase YAML configuration.
    monkeypatch.setenv("OCHECORE_CLIENT_ID", "")
    assert Settings(_env_file=None).client_id == "yaml-client"
    assert Settings(_env_file=None, client_id="explicit").client_id == "explicit"


def test_dotenv_overrides_yaml(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("port: 8091\n", encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("OCHECORE_PORT=8092\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "yaml_file", config_file)
    assert Settings(_env_file=env_file).port == 8092


def test_yaml_invalid_settings_fail_validation(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("port: 99999\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "yaml_file", config_file)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_saved_legacy_local_setting_is_ignored_without_losing_connection(settings):
    saved = {
        "client_id": "saved-client",
        "board_id": "",
        "local_board_url": "http://old-board:3180",
    }
    path = settings.data_dir / "connection.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert settings.connection().model_dump() == {"client_id": "saved-client", "board_id": ""}
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/config").json()["client_id"] == "saved-client"
        assert client.put("/api/config", json={"client_id": "saved-client"}).status_code == 200
    assert "local_board_url" not in json.loads(path.read_text(encoding="utf-8"))
