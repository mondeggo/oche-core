import asyncio
import json
import os
import stat
import time
from urllib.parse import urlsplit

import httpx
import pytest

from ochecore.autodarts.auth import DeviceAuth, safe_error
from ochecore.autodarts.errors import ConnectionProblem, LoginRequired
from ochecore.storage import write_private_json

TOKENS = {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 900}


def auth_for(http, tmp_path, client_id="ochecore-test"):
    return DeviceAuth(http, "https://api.autodarts.com", client_id, tmp_path / "tokens.json")


async def test_device_login_pending_then_success_and_reload(tmp_path):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        assert request.headers["content-type"] == "application/json"
        if request.url.path.endswith("/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private-device",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://auth.autodarts.com/link",
                    "expires_in": 600,
                    "interval": 0.01,
                },
            )
        if len(requests) == 2:
            return httpx.Response(400, json={"error": "authorization_pending"})
        assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
        assert body["device_code"] == "private-device"
        return httpx.Response(200, json=TOKENS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = auth_for(http, tmp_path)
        first = await auth.start_login()
        await auth.start_login()
        assert len(requests) == 1  # starting twice doesn't invalidate a pending code
        assert "private-device" not in json.dumps(first)
        await asyncio.wait_for(auth.poll_task, 2)
        assert auth.state == "authenticated"
        assert await auth.access_token() == "access-secret"
        assert "access-secret" not in json.dumps(auth.status())
        saved = json.loads(auth.path.read_text())
        assert saved["refresh_token"] == "refresh-secret"
        if os.name != "nt":
            assert stat.S_IMODE(auth.path.stat().st_mode) == 0o600
        assert await auth_for(http, tmp_path).access_token() == "access-secret"
        assert auth_for(http, tmp_path, "other-app").tokens is None
        await auth.forget()
        assert not auth.path.exists()


async def test_concurrent_refresh_rotates_only_once(tmp_path):
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        await asyncio.sleep(0.01)
        return httpx.Response(
            200, json={**TOKENS, "access_token": "new", "refresh_token": "rotated"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = auth_for(http, tmp_path)
        auth._save(TOKENS)
        results = await asyncio.gather(
            *[auth.access_token(rejected="access-secret") for _ in range(8)]
        )
        assert results == ["new"] * 8
        assert calls == [{"client_id": "ochecore-test", "refresh_token": "refresh-secret"}]
        assert json.loads(auth.path.read_text())["refresh_token"] == "rotated"


async def test_expired_refresh_requires_login_without_leaking_upstream_error(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(400, json={"error": "invalid_grant", "description": "SECRET"})
        )
    ) as http:
        auth = auth_for(http, tmp_path)
        auth._save({**TOKENS, "expires_in": 1})
        with pytest.raises(LoginRequired):
            await auth.access_token()
        assert auth.tokens is None
        assert not auth.path.exists()
        assert "SECRET" not in json.dumps(auth.status())


@pytest.mark.parametrize("error", ["access_denied", "expired_token", "unauthorized_client"])
async def test_device_terminal_errors(tmp_path, error):
    def handler(request):
        if urlsplit(str(request.url)).path.endswith("/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private",
                    "user_code": "PUBLIC",
                    "verification_uri": "https://auth.autodarts.com/link",
                    "expires_in": 10,
                    "interval": 0.001,
                },
            )
        return httpx.Response(400, json={"error": error})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = auth_for(http, tmp_path)
        await auth.start_login()
        await asyncio.wait_for(auth.poll_task, 1)
        assert auth.state == "error"
        assert auth.device is None
        assert not auth.path.exists()


async def test_slow_down_obeys_increased_interval(tmp_path, monkeypatch):
    sleeps = []
    poll_count = 0

    async def fake_sleep(delay):
        sleeps.append(delay)

    def handler(request):
        nonlocal poll_count
        if request.url.path.endswith("/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "private",
                    "user_code": "PUBLIC",
                    "verification_uri": "https://auth.autodarts.com/link",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        poll_count += 1
        if poll_count == 1:
            return httpx.Response(400, json={"error": "slow_down"})
        return httpx.Response(200, json=TOKENS)

    monkeypatch.setattr("ochecore.autodarts.auth.asyncio.sleep", fake_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = auth_for(http, tmp_path)
        await auth.start_login()
        await auth.poll_task
        assert sleeps == [5, 10]
        assert auth.tokens.expires_at > time.time()


async def test_corrupt_token_file_is_recoverable(tmp_path):
    (tmp_path / "tokens.json").write_text("not json")
    async with httpx.AsyncClient() as http:
        auth = auth_for(http, tmp_path)
        assert auth.tokens is None
        assert auth.error


async def test_transient_refresh_failure_keeps_refresh_token(tmp_path):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(503, json={"error": "unavailable"}))
    ) as http:
        auth = auth_for(http, tmp_path)
        auth._save({**TOKENS, "expires_in": 1})
        with pytest.raises(ConnectionProblem):
            await auth.access_token()
        assert auth.tokens.refresh_token.get_secret_value() == "refresh-secret"
        assert auth.path.exists()


def test_network_and_storage_errors_are_distinct(tmp_path):
    assert "Network" in safe_error(ConnectionRefusedError("sensitive socket details"))
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("test")
    with pytest.raises(ConnectionProblem, match="data directory"):
        write_private_json(blocking_file / "tokens.json", {})
