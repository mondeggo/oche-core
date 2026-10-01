# Phase 1 validation

Updated October 2, 2026 after separating HTTP routes and WebSocket streaming from application
setup. Terminal controls and the optional UI share the same headless service.

## Automated checks

**57 tests passed**, covering OAuth approval/errors, polling slowdown, concurrent refresh,
token rotation/storage, expired or corrupt sessions, cloud bootstrap, match changes, stale
events, HTTP 401 retry, local WebSocket test-server reconnection, bounded
dispatch, API configuration, origin checks and WebSocket cleanup.

Coverage includes YAML loading, environment/.env precedence, empty environment values,
locked fields, malformed settings, message parsing and match-state tracking. New ticket
checks cover a fresh ticket on every connection, query encoding, absence of bearer headers
on the WebSocket, a 401 refresh/retry, invalid ticket responses and gateway errors.

Terminal tests cover configuration persistence and locks, device approval and denial,
session restoration, logout, nonblocking login, event history and live JSON streaming.
API tests run with the UI disabled and its static directory unavailable. A migration test
checks that old local-board settings do not block startup or survive the next config save.

Board-discovery tests cover authenticated access through API/CLI with no board selected,
multiple boards, empty accounts, invalid responses, token refresh after 401, upstream errors
and omission of private upstream fields. Discovery leaves saved configuration unchanged.

The complete suite passed after moving routes into `http.py` and `websocket.py`, including
HTTP configuration, headless CLI access, live WebSocket delivery and subscriber cleanup.

Ruff checks and formatting passed for `src/ochecore/` and `tests/`. The simplified UI passed
JavaScript syntax validation. No new dependencies were added.

## Deployment

`docker compose up --build -d --wait --wait-timeout 60` successfully rebuilt the image from `src/ochecore/` and
recreated the container. Docker reports it as healthy, and `GET /healthz` returns
`{"status":"ok","version":"0.1.0"}` at `http://127.0.0.1:9180`.
The image retains UID/GID 10001 and the existing data volume.

The host CLI and container CLI successfully read the running service's status and
configuration. An isolated container with `OCHECORE_UI_ENABLED=false` returned 404 for
`/`, `/static/app.js` and `/docs`, while `/healthz`, `/api/status` and `/openapi.json`
returned 200 and `ochecore status` succeeded. The isolated container was stopped and
automatically removed after verification. The regular instance retains the optional UI.

## Windows temporary-directory permissions

Changing the agent execution context left its earlier pytest temporary/cache directories
inaccessible. This review ran tests with a fresh directory in the Windows temporary folder,
disabled the pytest cache and disabled bytecode writes to avoid adding generated project
files. System permissions were not changed.

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$testRunDir = Join-Path ([System.IO.Path]::GetTempPath()) ('ochecore-tests-' + [guid]::NewGuid().ToString('N'))
uv run pytest -q -p no:cacheprovider --basetemp $testRunDir
```

Starlette emits one deprecation warning about its httpx test client. Tests pass; this is
not a runtime connection failure.

## Live connection

The running Docker service restored the connected account session and discovered one online
board through `ochecore boards`. Selecting it with `ochecore config --board-id ...` persisted
the configuration and connected the cloud stream. `/readyz` returned 200. The service then
detected an active match and received three cloud events. These checks used the configured
development Client ID, `darts-caller`.

After the route refactor and Docker rebuild on October 2, the service restored authentication
and reconnected to the selected board's cloud stream without another login.

## Remaining verification

- Gameplay acceptance: verify throw payloads, corrections, match changes and recovery during
  a real match. Receiving initial cloud events does not establish complete gameplay coverage.
- Repeat authentication with an OAuth Client ID assigned to OcheCore before distribution.
- Visual/browser acceptance: the browser tool previously denied local access to
  `http://127.0.0.1:9180`. No alternative browser automation bypassed that decision.
- ARM/Raspberry Pi: Docker was tested on Linux amd64 through Docker Desktop.
- Long-running sessions: HTTP refresh and WebSocket recovery are tested separately.
  Real acceptance should include a match beyond 15 minutes and an application restart.
