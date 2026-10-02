# Connection and event validation

Updated October 2, 2026 after simplifying the web interface and removing the unused combined
event history and subscriber queue. Terminal controls and the optional UI share the same
headless service.

## Interface refresh

The sidebar separates Overview, Events and Integrations, with AutoDarts settings and clearly
marked planned WLED/Caller pages. Advanced connection settings and JSON payloads use
expandable details. The layout adapts to narrow screens, and navigation provides keyboard
focus, current-page labels and a skip link.

All 85 Python tests, Ruff checks, formatting and JavaScript syntax checks passed after the
cleanup. A temporary jsdom check with mocked API responses also passed navigation, settings
saves, locked fields, preservation of unsaved edits and expanded events, raw/normalized
switching, delayed-response handling, offline recovery and device approval. Raw duplicate,
null and string frames were checked, including rendering upstream markup as plain text.
This was a DOM behavior check, not a visual browser review. No project dependencies were added.

`uv build` successfully produced the source distribution and wheel, and
`docker compose config --quiet` passed. The Docker rebuild could not run: Docker Desktop was
stopped, and starting it failed in the inference manager while opening its `dockerInference`
socket. The redesigned interface has **not** been deployed to Docker yet. After Docker Desktop
is working, run `docker compose up --build -d --wait --wait-timeout 60`.

Browser validation also remains pending: the browser tool returned no available browsers.
The deployment checks below describe the earlier successful event/API release.

## Automated checks

Development binding was checked after changing the startup configuration to `0.0.0.0`:
the uv settings resolve to that address, and Compose resolves the development publication to
`0.0.0.0:9180`. The 15 configuration/CLI tests and Ruff passed, and the Bash launcher passed
syntax validation. Docker deployment remains pending because the engine is unavailable.

**85 tests passed**, covering OAuth approval/errors, polling slowdown, concurrent refresh,
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

The X01 replay tests cover newest-first visits, individual and batched darts, corrections,
replacement/restoration of dart IDs, undo, miss versus bust, winner index zero, repeated finish
messages, leg changes, roster reordering, takeout ownership and silent reconnection snapshots.
Fixtures are synthetic and derived from the supplied resources, not live scoring recordings.

API/CLI tests verify the separate normalized and raw routes, preservation of unknown upstream
fields, raw-frame credential redaction, independent bounded queues, and headless operation.
A cloud integration test checks that REST snapshots are excluded from the raw stream and
live full states yield normalized events without duplicates after reconnecting.
Subscription-error tests verify raw preservation, optional user-topic warnings, degraded
readiness for rejected game topics, continued delivery on healthy topics and error recovery.

Ruff checks and formatting passed for `src/ochecore/` and `tests/`. The simplified UI passed
JavaScript syntax validation. No new dependencies were added.

## Previous deployment checks

`docker compose up --build -d --wait --wait-timeout 60` successfully rebuilt the image from `src/ochecore/` and
recreated the container. Docker reports it as healthy, and `GET /healthz` returns
`{"status":"ok","version":"0.1.0"}` at `http://127.0.0.1:9180`.
The image retains UID/GID 10001. The service now binds `./data` to `/data`.
The user copied the stopped container's data to that folder after automatic approval review
blocked the agent's migration command. Docker inspection confirmed a bind mount, saved login
restored successfully, and the original named volume was retained. Tokens and connection
files are ignored by Git and excluded from the Docker build context.

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

The raw stream exposed an `unauthorized client` response for `autodarts.users` with the
development Client ID. Status now lists that optional subscription failure instead of
counting it as malformed input. No board-topic rejection was observed; full game delivery
still needs live acceptance. Both event history endpoints and the host/container CLI were
checked against the rebuilt service.

## Remaining verification

- Gameplay acceptance: verify throw payloads, corrections, match changes and recovery during
  a real match. Receiving initial cloud events does not establish complete gameplay coverage.
  The prior match-state endpoint returned 404 during this change; current normalization
  coverage is based on reference contracts and replay fixtures. See [event evidence](EVENTS.md).
- Repeat authentication with an OAuth Client ID assigned to OcheCore before distribution.
- Rebuild Docker and review the redesigned interface in a browser, including a narrow viewport.
  The earlier browser attempt denied localhost access; the latest attempt found no browser.
- ARM/Raspberry Pi: Docker was tested on Linux amd64 through Docker Desktop.
- Long-running sessions: HTTP refresh and WebSocket recovery are tested separately.
  Real acceptance should include a match beyond 15 minutes and an application restart.
