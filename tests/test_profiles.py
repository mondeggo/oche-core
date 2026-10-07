import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ochecore.autodarts.errors import ConnectionProblem
from ochecore.integrations.caller.service import CallerConfig
from ochecore.integrations.caller.voices import VOICES, VoiceLibrary
from ochecore.integrations.wled.service import WLED, Device, LightingProfile, Target, WLEDConfig
from ochecore.main import create_app
from ochecore.runtime import Runtime


@pytest.fixture
def profile_app(settings, monkeypatch):
    # Installed voices need no download or host playback during configuration tests.
    monkeypatch.setattr(VoiceLibrary, "installed", lambda self, voice: bool(voice))

    def no_network(request):
        pytest.fail(f"Profile configuration attempted network access: {request.url.path}")

    return create_app(settings, transport=httpx.MockTransport(no_network))


def lighting_config():
    return WLEDConfig(
        devices=[
            Device(
                id="board",
                name="Board",
                url="http://wled.test",
                enabled=False,
                targets=[Target(id="ring", name="Ring")],
            )
        ]
    ).model_dump(mode="json")


def test_profiles_restore_lighting_and_all_caller_settings(profile_app, settings):
    original_caller = CallerConfig(
        enabled=True,
        voice=next(iter(VOICES)),
        output="browser",
        volume=0.4,
        darts="segment",
        turn_totals=False,
        checkouts=False,
        players=False,
        include_bots=False,
        local_only=True,
    ).model_dump()
    with TestClient(profile_app) as client:
        assert client.put("/api/wled", json=lighting_config()).status_code == 200
        assert client.put("/api/caller", json=original_caller).status_code == 200
        quiet = client.post("/api/profiles", json={"name": "Quiet"}).json()["active_profile"]
        assert (
            client.patch("/api/caller", json={"enabled": False, "volume": 0.1}).status_code == 200
        )
        wled = client.get("/api/wled").json()
        wled["devices"][0]["targets"][0]["phases"]["ready"]["color"] = "#112233"
        # Device addressing and automation switches remain shared across profiles.
        wled["devices"][0]["name"] = "Renamed board"
        wled["devices"][0]["targets"][0]["segment"] = 2
        wled["enabled"] = True
        assert client.put("/api/wled", json=wled).status_code == 200
        assert client.put("/api/profile", json={"id": "default"}).status_code == 200
        assert client.get("/api/caller").json() == original_caller
        restored = client.get("/api/wled").json()
        assert restored["enabled"] is True
        assert restored["devices"][0]["name"] == "Renamed board"
        assert restored["devices"][0]["targets"][0]["segment"] == 2
        assert restored["devices"][0]["targets"][0]["phases"]["ready"]["color"] == "#00ff00"
        assert client.put("/api/profile", json={"id": quiet}).status_code == 200
        assert client.get("/api/caller").json()["volume"] == 0.1
        assert client.get("/api/caller").json()["enabled"] is False
        assert (
            client.get("/api/wled").json()["devices"][0]["targets"][0]["phases"]["ready"]["color"]
            == "#112233"
        )
    assert (settings.data_dir / "profiles.json").is_file()
    assert not (settings.data_dir / "wled.json").exists()
    assert not (settings.data_dir / "caller.json").exists()
    with TestClient(profile_app) as client:
        assert client.get("/api/profiles").json()["active_profile"] == quiet
        assert client.get("/api/caller").json()["volume"] == 0.1


def test_blank_profile_is_silent_and_keeps_hardware(profile_app):
    with TestClient(profile_app) as client:
        client.put("/api/wled", json=lighting_config())
        client.patch("/api/caller", json={"enabled": True, "voice": next(iter(VOICES))})
        response = client.post("/api/profiles", json={"name": "Blank", "source": "blank"})
        assert response.status_code == 200
        assert client.get("/api/caller").json() == CallerConfig().model_dump()
        device = client.get("/api/wled").json()["devices"][0]
        assert device["url"] == "http://wled.test" and device["enabled"] is False
        target = device["targets"][0]
        assert all(phase["brightness"] == 0 for phase in target["phases"].values())
        assert target["effects"] == target["players"] == {}
        assert target["matrix"]["appearance"]["brightness"] == 0


def test_existing_profiles_migrate_without_modifying_legacy_backups(profile_app, settings):
    config = WLEDConfig.model_validate(lighting_config())
    config.profiles.append(LightingProfile(id="quiet", name="Quiet"))
    WLED.sync_profiles(config)
    config.profiles[1].targets["board"]["ring"].phases.ready.color = "#123456"
    WLED.apply_profile(config, config.profiles[1])
    legacy = {
        "wled.json": json.dumps(config.model_dump(mode="json")),
        "caller.json": json.dumps(CallerConfig(volume=0.25, output="browser").model_dump()),
    }
    for name, content in legacy.items():
        (settings.data_dir / name).write_text(content, encoding="utf-8")
    with TestClient(profile_app) as client:
        assert client.get("/api/profiles").json() == {
            "active_profile": "quiet",
            "profiles": [{"id": "default", "name": "Default"}, {"id": "quiet", "name": "Quiet"}],
        }
        assert client.get("/api/caller").json()["volume"] == 0.25
        assert (
            client.get("/api/wled").json()["devices"][0]["targets"][0]["phases"]["ready"]["color"]
            == "#123456"
        )
        assert client.put("/api/profile", json={"id": "default"}).status_code == 200
        assert client.get("/api/caller").json()["volume"] == 0.25
        client.patch("/api/caller", json={"volume": 0.75})
    for name, content in legacy.items():
        assert (settings.data_dir / name).read_text(encoding="utf-8") == content
    with TestClient(profile_app) as client:
        assert client.get("/api/profiles").json()["active_profile"] == "default"
        assert client.get("/api/caller").json()["volume"] == 0.75
        client.put("/api/profile", json={"id": "quiet"})
        assert client.get("/api/caller").json()["volume"] == 0.25


def test_wled_profile_aliases_share_the_application_selection(profile_app):
    with TestClient(profile_app) as client:
        quiet = client.post("/api/wled/profiles", json={"name": "Quiet"}).json()["active_profile"]
        client.patch("/api/caller", json={"volume": 0.2})
        assert client.put("/api/wled/profile", json={"id": "default"}).status_code == 200
        assert client.get("/api/caller").json()["volume"] == 0.6
        assert client.put("/api/profile", json={"id": quiet}).status_code == 200
        assert client.get("/api/caller").json()["volume"] == 0.2
        assert client.delete(f"/api/wled/profiles/{quiet}").status_code == 200
        assert client.get("/api/caller").json()["volume"] == 0.6
        assert client.get("/api/profiles").json()["active_profile"] == "default"
        assert client.delete("/api/profiles/default").status_code == 409


def test_profile_revision_tracks_integrations_and_switch_invalidates_both_scopes(profile_app):
    with TestClient(profile_app) as client:
        revision = client.get("/api/profiles").headers["etag"]
        initial_caller = client.get("/api/caller").headers["etag"]
        changed = client.patch(
            "/api/caller", json={"volume": 0.2}, headers={"If-Match": initial_caller}
        )
        assert changed.status_code == 200
        assert (
            client.post(
                "/api/profiles", json={"name": "Stale"}, headers={"If-Match": revision}
            ).status_code
            == 412
        )
        second = client.get("/api/profiles").headers["etag"]
        wled_revision = client.get("/api/wled").headers["etag"]
        caller_revision = changed.headers["etag"]
        assert client.get("/api/wled/status").headers["etag"] == wled_revision
        assert client.get("/api/caller/status").headers["etag"] == caller_revision
        created = client.post(
            "/api/profiles", json={"name": "Practice"}, headers={"If-Match": second}
        )
        assert created.status_code == 200
        for path, body, etag in (
            ("/api/caller", {"volume": 0.8}, caller_revision),
            ("/api/wled", {"enabled": True}, wled_revision),
        ):
            assert client.patch(path, json=body, headers={"If-Match": etag}).status_code == 412
        assert (
            client.put(
                "/api/profile", json={"id": "default"}, headers={"If-Match": second}
            ).status_code
            == 412
        )
        profile_id = created.json()["active_profile"]
        assert (
            client.delete(f"/api/profiles/{profile_id}", headers={"If-Match": second}).status_code
            == 412
        )
        assert client.get("/api/caller").json()["volume"] == 0.2


def test_unrelated_caller_and_wled_saves_do_not_conflict(profile_app):
    with TestClient(profile_app) as client:
        wled_revision = client.get("/api/wled").headers["etag"]
        caller_revision = client.get("/api/caller").headers["etag"]
        first = client.patch(
            "/api/caller", json={"volume": 0.2}, headers={"If-Match": caller_revision}
        )
        assert first.status_code == 200
        assert client.get("/api/wled").headers["etag"] == wled_revision
        second = client.patch(
            "/api/wled", json={"enabled": True}, headers={"If-Match": wled_revision}
        )
        assert second.status_code == 200
        assert client.get("/api/caller").headers["etag"] == first.headers["etag"]
        assert (
            client.patch(
                "/api/caller", json={"volume": 0.3}, headers={"If-Match": first.headers["etag"]}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                "/api/caller", json={"volume": 0.9}, headers={"If-Match": caller_revision}
            ).status_code
            == 412
        )
        assert (
            client.patch(
                "/api/wled", json={"enabled": False}, headers={"If-Match": wled_revision}
            ).status_code
            == 412
        )


def test_legacy_profile_and_install_routes_use_their_integration_revision(profile_app):
    with TestClient(profile_app) as client:
        wled_revision = client.get("/api/wled").headers["etag"]
        client.patch("/api/caller", json={"volume": 0.2})
        created = client.post(
            "/api/wled/profiles", json={"name": "Quiet"}, headers={"If-Match": wled_revision}
        )
        assert created.status_code == 200
        assert created.headers["etag"] == client.get("/api/wled").headers["etag"]
        caller_revision = client.get("/api/caller").headers["etag"]
        client.patch("/api/wled", json={"enabled": True})
        voice_id = next(iter(VOICES))
        installed = client.post(
            f"/api/caller/voices/{voice_id}/install",
            json={},
            headers={"If-Match": caller_revision},
        )
        assert installed.status_code == 202
        assert installed.headers["etag"] == client.get("/api/caller").headers["etag"]
        client.put("/api/profile", json={"id": "default"})
        assert (
            client.post(
                f"/api/caller/voices/{voice_id}/install",
                json={},
                headers={"If-Match": installed.headers["etag"]},
            ).status_code
            == 412
        )


def test_invalid_voice_does_not_persist_or_change_active_profile(profile_app, settings):
    with TestClient(profile_app) as client:
        before = client.get("/api/profiles")
        assert client.patch("/api/caller", json={"voice": "unknown"}).status_code == 409
        assert client.get("/api/profiles").headers["etag"] == before.headers["etag"]
        assert not (settings.data_dir / "profiles.json").exists()


def test_installation_blocks_switch_before_lighting_or_store_changes(profile_app, settings):
    with TestClient(profile_app) as client:
        client.patch("/api/caller", json={"voice": next(iter(VOICES))})
        quiet = client.post("/api/profiles", json={"name": "Quiet", "source": "blank"}).json()[
            "active_profile"
        ]
        client.put("/api/profile", json={"id": "default"})
        before = (settings.data_dir / "profiles.json").read_bytes()

        async def pretend_installation():
            library = profile_app.state.runtime.caller.library
            library.download["voice_id"] = profile_app.state.runtime.caller.config.voice
            library.task = asyncio.create_task(asyncio.Event().wait())

        client.portal.call(pretend_installation)
        response = client.put("/api/profile", json={"id": quiet})
        assert response.status_code == 409
        assert "installation" in response.json()["detail"]
        assert client.get("/api/profiles").json()["active_profile"] == "default"
        assert client.get("/api/wled").json()["active_profile"] == "default"
        assert (settings.data_dir / "profiles.json").read_bytes() == before


def test_write_failure_keeps_both_integrations_and_revision(profile_app, settings, monkeypatch):
    with TestClient(profile_app) as client:
        client.patch("/api/caller", json={"volume": 0.25})
        before = (settings.data_dir / "profiles.json").read_bytes()
        revision = client.get("/api/profiles").headers["etag"]

        def fail_write(*args):
            raise ConnectionProblem("Cannot write application data.")

        monkeypatch.setattr("ochecore.profiles.write_private_json", fail_write)
        response = client.post("/api/profiles", json={"name": "Blank", "source": "blank"})
        assert response.status_code == 409
        assert client.get("/api/profiles").headers["etag"] == revision
        assert client.get("/api/wled").json()["active_profile"] == "default"
        assert client.get("/api/caller").json()["volume"] == 0.25
        assert (settings.data_dir / "profiles.json").read_bytes() == before


def test_apply_failure_restores_the_persisted_profile(profile_app, settings, monkeypatch):
    with TestClient(profile_app) as client:
        client.patch("/api/caller", json={"volume": 0.25})
        before = json.loads((settings.data_dir / "profiles.json").read_text())
        service = profile_app.state.runtime.caller
        configure = service.configure
        calls = 0

        async def fail_once(config, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionProblem("Temporary playback failure")
            return await configure(config, **kwargs)

        monkeypatch.setattr(service, "configure", fail_once)
        assert (
            client.post("/api/profiles", json={"name": "Blank", "source": "blank"}).status_code
            == 409
        )
        assert calls == 2
        assert client.get("/api/wled").json()["active_profile"] == "default"
        assert client.get("/api/caller").json()["volume"] == 0.25
        assert client.get("/api/profiles").json()["profiles"] == [
            {"id": "default", "name": "Default"}
        ]
        assert json.loads((settings.data_dir / "profiles.json").read_text()) == before


async def test_cancelled_profile_change_finishes_rollback_before_returning(settings, monkeypatch):
    def no_network(request):
        pytest.fail(f"Profile rollback attempted network access: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_network)) as http:
        runtime = Runtime(settings, http)
        await runtime.profiles.configure_caller(CallerConfig(volume=0.25))
        before = runtime.profiles.path.read_bytes()
        revision = runtime.profiles.revision
        configure = runtime.caller.configure
        applying, rolling_back, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0

        async def pause_apply_and_rollback(config, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                applying.set()
                await asyncio.Event().wait()
            if calls == 2:
                rolling_back.set()
                await release.wait()
            return await configure(config, **kwargs)

        monkeypatch.setattr(runtime.caller, "configure", pause_apply_and_rollback)
        task = asyncio.create_task(runtime.profiles.create("Interrupted", "blank"))
        try:
            await asyncio.wait_for(applying.wait(), 1)
            assert runtime.wled.config.active_profile != "default"
            task.cancel()
            await asyncio.wait_for(rolling_back.wait(), 1)
            task.cancel()  # A second cancellation must not interrupt restoration.
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert runtime.profiles.path.read_bytes() == before
            assert runtime.profiles.revision == revision
            assert runtime.wled.config.active_profile == "default"
            assert runtime.caller.config.volume == 0.25
            assert runtime.profiles.summary()["profiles"] == [{"id": "default", "name": "Default"}]
            # The next mutation must use coherent lighting and Caller profile IDs.
            created = await runtime.profiles.create("Next")
            assert created["active_profile"] == runtime.wled.config.active_profile
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runtime.close()


@pytest.mark.parametrize("corrupt", ["broken", '{"version":1,"wled":{},"callers":{}}'])
def test_invalid_authoritative_store_is_not_replaced_with_legacy_data(
    profile_app, settings, corrupt
):
    path = settings.data_dir / "profiles.json"
    path.write_text(corrupt, encoding="utf-8")
    (settings.data_dir / "caller.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ConnectionProblem, match="Restore a valid backup"):
        with TestClient(profile_app):
            pass
    assert path.read_text(encoding="utf-8") == corrupt
