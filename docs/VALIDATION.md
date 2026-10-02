# Connection, events and integration validation

Updated October 2, 2026 after implementing Caller. Terminal controls and the optional
UI share the same headless service.

## Caller

All **150 Python tests** and Ruff checks pass. The built wheel includes Caller, the voice
catalogue and UI assets. Compose configuration validates. The native service was restarted
on `0.0.0.0:9180`; health, UI assets, caller status/configuration/catalogue and the caller CLI
passed live checks. An empty PATCH verified routing without changing saved settings.

Backend tests cover all 14 modes from Tools for AutoDarts, CountUp aliases and Cricket/Tactics
settings. The mode tests vary the common reference match envelope and check silent baselines,
visit scores, wins and reconnects. Separate cases cover checkout reachability, target context,
win/bust priority, bot/local filters, correction cancellation, stale events, serial playback,
headless API/CLI controls, settings persistence, archive limits and safe file paths.

The French Rémi pack was downloaded from the provider: its outer ZIP contains a CSV and an
audio ZIP with 18,913 MP3 files. Installation produced 16,757 sound keys, including aliases and
`+N` variants. Real score, bust, win and triple clips decoded successfully with SDL's dummy
audio device; this does not validate physical speakers. The installed pack is in ignored
`data/voices/`; Caller remains disabled by default.

Temporary DOM checks passed Caller activation/navigation, French voice selection, download
progress, configuration save, manual tests, draft preservation, external API changes and
failed/offline controls. Browser rendering and audible playback remain to be checked manually.
The Docker rebuild was attempted again and timed out before producing build output.

## Integration navigation and activation

AutoDarts now sits with Overview and Events in the main navigation. The Integrations heading
is separated by a divider; All integrations contains activation switches. WLED appears in
the sidebar only while enabled. Caller now follows the same activation behavior.

`PATCH /api/wled` merges supplied top-level configuration fields under the existing service
lock. UI switches and CLI activation change only `enabled`; device saves replace only the
device list. Saving an older form cannot undo a separate integration-disable request.
Saved configuration survives disable/re-enable and service restarts.

Before Caller was added, all **119 Python tests** passed, including partial-update preservation, restart, validation,
origin and content-type checks. Temporary DOM checks passed activation, conditional navigation,
disabled deep links, external CLI changes, retained form drafts, failed/duplicate switch
requests, offline recovery and the existing WLED discovery/editor flows. Theme checks still
pass. These are programmatic checks; visual browser review remains pending as described below.

The native service was restarted on `0.0.0.0:9180`. Health and updated UI assets passed live
checks; an empty PATCH confirmed the new route without changing integration settings. The
wheel contains the updated assets. Compose configuration passed, but the Docker build timed out.

## Dark and light themes

Both palettes retain Oche's red and cream identity. Dark mode uses softer foregrounds,
distinct primary/secondary text, less saturated accents and separate button/input borders.
Light mode uses warm neutral surfaces with the same red identity. Disabled controls use
explicit colours instead of fading the entire element, and the current navigation item has
an accent marker. The [Oche handoff](oche-theme.css) maps the values to Oche's variable names.

The preference is stored in the browser and applied before the stylesheet loads. The content
column is centered beside the sidebar, and the switch remains available on narrow screens.

Temporary jsdom checks passed default/saved themes, switching and reloads, blocked storage,
cross-tab changes, switch accessibility attributes and text contrast in both palettes.
The contrast refinement also checked hover/selected/notice text, placeholders, input borders
and focus rings, plus agreement between the handoff file and the application palette.
Main/secondary text against cards measures 11.40:1 / 5.93:1 in dark mode and 13.08:1 / 5.67:1
in light mode. Primary button labels measure 5.36:1 and 6.30:1 respectively. The checked active
text pairs exceed 4.5:1; input borders and focus indicators exceed 3:1 against their backgrounds.
The existing UI and WLED DOM checks also passed. The 31 API/CLI tests, Ruff checks, Python
formatting, JavaScript syntax, package builds and Compose configuration validation passed.
The wheel includes the updated HTML, CSS and new `theme.js`, without new dependencies.

The running native service still listens on `0.0.0.0:9180`. Health and all changed assets
returned HTTP 200, and served assets matched the source files. Visual browser review remains
pending: the browser plugin exposed no browsers, and the Windows preview tool stopped because
it could not verify the browser URL. No further browser automation was attempted. The Docker
rebuild timed out again; the native service has the update, while container deployment remains
pending.

## WLED discovery

All **117 Python tests** passed after adding discovery, along with Ruff, Python formatting,
JavaScript syntax checks, package builds and Compose configuration validation. Discovery uses
`zeroconf` with its `ifaddr` dependency; both are recorded in `uv.lock`.

New tests cover verified/deduplicated results, disappearing and invalid advertisements,
unreachable/non-WLED responses, bounded pending work, cancellation cleanup, network startup
errors, concurrent scans and API/CLI operation with the UI and lighting disabled. Discovery
does not save configuration or send lighting commands.

The temporary jsdom checks also passed discovery loading/empty/error states, safe rendering
of advertised names, duplicate prevention by IP/hostname, explicit Add then Save, and preservation
of pending edits. Visual browser review remains pending.

The native development service was started with the updated code on `0.0.0.0:9180`.
`ochecore wled discover` completed a real mDNS scan and returned an empty device list.
Health, UI and WLED JavaScript requests returned HTTP 200. This verifies the scan runs in this
environment; live identification of an installed controller still needs an advertising device
reachable from the service. Docker deployment remains pending as described below.

## First WLED implementation

All **112 Python tests** passed. The 27 WLED cases cover configuration validation and persistence,
overlapping targets, segment bounds, reserved effects, matrix rotation and wiring, priority
and expiry, corrections, rejected device responses, independent workers, timed previews,
shutdown/disable cleanup and headless API/CLI control. Phase tests include remote takeout,
unknown readiness, malformed state, stale board status and reconnect baselines.

Ruff, Python formatting, JavaScript syntax checks, wheel/source builds, sample configuration
validation and `docker compose config --quiet` passed. The wheel includes `wled.py` and
`static/wled.js`; no runtime dependencies were added.

Temporary jsdom checks with mocked API responses passed WLED device/target editing, probes,
phase and matrix previews (including zero), saved/unsaved forms, device navigation, disabled
controls and offline recovery. Existing UI regression checks also passed. These verify DOM
behaviour; visual verification is pending because the browser tool has no available browsers.

No real WLED controller was configured or contacted. Verify the selected segments and matrix
orientation on the installed hardware, then check live ready/takeout/wait transitions and
corrections during a match. The recognised readiness strings come from supplied references;
their presence in the current AutoDarts v2 cloud stream has not been established.

Docker deployment remains pending. Both the engine probe and Compose build timed out, following
the earlier Docker Desktop startup failure described below. Rebuild with
`docker compose up --build -d --wait --wait-timeout 60` once the engine is working.

## Interface refresh

The sidebar separates Overview, Events and Integrations, with AutoDarts settings, WLED controls
and a clearly marked planned Caller page. Advanced connection settings and JSON payloads use
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

The original **85 tests** cover OAuth approval/errors, polling slowdown, concurrent refresh,
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
JavaScript syntax validation. The earlier UI cleanup added no dependencies; discovery now uses
`zeroconf` as described above.

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
