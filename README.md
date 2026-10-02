# OcheCore

Headless AutoDarts event gateway built with Python, uv and Docker.

OAuth device login, board discovery, automatic reconnection and normalized game events,
with separate raw AutoDarts streams. A small optional web interface uses the same API as the CLI.
The service owns authentication, persistence and event processing and keeps running when
either client closes. It starts without a configured board. WLED supports phase lighting,
temporary game effects and numeric matrix scores. Caller supports downloadable voices,
host/browser playback and all 14 reference game modes. MQTT and Home Assistant remain planned.
Event interpretation is tested against the supplied contracts; full live gameplay acceptance
remains to be done.

- [Project plan](docs/PLAN.md)
- [API research](docs/AUTODARTS_API.md)
- [Validation notes](docs/VALIDATION.md)
- [Event API and normalization rules](docs/EVENTS.md)
- [Caller voices, game modes and playback](docs/CALLER.md)

## Structure

```text
OcheCore/
├── src/
│   └── ochecore/
│       ├── main.py          # application setup and lifecycle
│       ├── http.py          # HTTP controls, health checks and optional UI route
│       ├── websocket.py     # live event streaming and subscriber cleanup
│       ├── cli.py           # terminal controls over the service API
│       ├── config.py        # validated settings
│       ├── runtime.py       # connection lifecycle
│       ├── storage.py       # atomic persistence
│       ├── events.py        # parsing, game event normalization and bounded dispatch
│       ├── wled.py          # controllers, lighting targets, effects and matrix rendering
│       ├── caller.py        # game announcements and host/browser audio controls
│       ├── voices.py        # voice installation and local sound lookup
│       ├── voices.json      # provider voice catalogue
│       ├── autodarts/
│       │   ├── auth.py
│       │   ├── cloud.py     # cloud connection and match tracking
│       │   └── errors.py
│       └── static/          # optional minimal control interface
├── config/config.yaml
├── tests/
├── docs/
├── pyproject.toml
├── uv.lock
├── Dockerfile
└── docker-compose.yml
```

All application source lives under `src/ochecore/`. `main.py` assembles the application;
`http.py` handles HTTP routes and `websocket.py` handles live event streaming. Event handling
lives in `events.py` and AutoDarts connections in `autodarts/`. Future integrations will get
modules when they are implemented.
OcheCore uses the AutoDarts cloud. The legacy local-board adapter has been removed.

## Start with uv

```powershell
uv sync --locked
uv run ochecore serve --no-ui
```

This starts the API and event stream at <http://127.0.0.1:9180> with web pages disabled.
The checked-in configuration listens on `0.0.0.0`, so other devices on your network can use
`http://YOUR_LAN_IP:9180` as well. `0.0.0.0` is the listening address; use the computer's actual
IP address in a browser.
Use `uv run ochecore serve` to respect `ui_enabled` in the configuration (enabled by default).
Running `uv run ochecore` also starts the service. Run commands from the project root. uv manages `.venv`;
`.python-version` selects Python 3.13, and the package supports Python 3.12 or newer.
`uv run python -m ochecore.main` accepts the same commands.

## Control from the terminal

Keep the service running in Docker or another terminal. These commands control that service:

```powershell
uv run ochecore status
uv run ochecore config
uv run ochecore login
uv run ochecore boards
uv run ochecore config --board-id YOUR_BOARD_UUID
uv run ochecore events
uv run ochecore events --follow
uv run ochecore events --raw
uv run ochecore events --raw --follow
uv run ochecore events --debug on
uv run ochecore events --debug off
uv run ochecore logout
```

`login` prints the AutoDarts URL and user code, then waits for approval. Approve on your phone
or another computer; the OcheCore host needs no browser. Use `login --no-wait --json` to return
immediately while the service continues approval polling. Ctrl+C also leaves polling active.
`logout` clears the service's saved session.

`boards` discovers boards linked to the connected AutoDarts account and prints each ID,
name and online status. It works before a board is selected. Discovery never changes the
selected board; use `config --board-id` or choose a board in the UI and save. An empty list
means the account has no boards; login and upstream errors are reported separately.

`config` updates only the supplied fields and applies the same locks and validation as the UI.
Use `--client-id YOUR_CLIENT_ID` to update an unlocked client field, or `--board-id=` to clear
the selected board. Status, configuration and event history use JSON. Live events use one JSON
object per line; a lost stream exits with an error so a supervisor can restart the command.
Commands return nonzero on failure and 130 on interruption.

`events` reads normalized game events. Add `--raw` to read incoming AutoDarts WebSocket
frames with their original JSON structure. Both streams have independent histories and
subscriber queues. Raw frames include duplicates and control messages; REST snapshots are
not inserted into the raw stream. Known credential fields are redacted.

Each history keeps 100 entries in memory. `events --debug on` records future raw frames to
`data/debug/*.jsonl` until `events --debug off` or service shutdown. Use `events --debug status`
to see the file path, counts and errors. The Events page has the same switch and a download
link for the stopped capture. See [debug recording](docs/EVENTS.md#debug-recording).

The default control address is `http://127.0.0.1:9180`. Override it with the `OCHECORE_URL`
environment variable or a flag before the command:

```powershell
uv run ochecore --url http://127.0.0.1:9280 status
```

## Start with Docker

```powershell
docker compose up --build -d
docker compose logs -f
```

The same interface is available at <http://127.0.0.1:9180>. The container runs as a non-root
user, stores persistent data in the project's `./data` folder and mounts `config/` read-only.
AutoDarts Detection runs separately on the board computer; OcheCore does not access cameras.
The `./data:/data` bind mount contains connection/integration settings, voices and OAuth tokens. It is
excluded from Git and Docker build context. Back up this folder to preserve the login.
On Linux, create it before startup and give container UID/GID 10001 write access:
`mkdir -p data && sudo chown 10001:10001 data && chmod 700 data`.

To migrate an existing named volume, stop the old service, copy its `/data` contents into
the initially absent `./data` folder with `docker cp CONTAINER:/data ./data`, then recreate
the service with this Compose configuration. Keep the original volume until the migrated
service has successfully restored its session.

Set `ui_enabled: false` in `config/config.yaml`, or `OCHECORE_UI_ENABLED=false` in `.env`,
to disable web pages in Docker. Restart after changing YAML; recreate with
`docker compose up -d` after changing environment variables. All controls also work inside
the container:

```powershell
docker compose exec ochecore ochecore status
docker compose exec ochecore ochecore login
docker compose exec ochecore ochecore boards
docker compose exec ochecore ochecore config --board-id YOUR_BOARD_UUID
docker compose exec ochecore ochecore events --follow
```

## Connect AutoDarts

1. Configure an OAuth Client ID with Device Authorization Grant enabled.
   The [integration guide](https://gist.github.com/lloydowen/960079f2b518f6f5d68e160465298964)
   lists `lloydowen` on Discord as the registration contact. This development checkout uses
   `darts-caller` as requested; replace it with OcheCore's assigned ID before distribution.
2. Run `ochecore login` or use **Connect account** in the optional UI. You can test login
   before installing a board.
3. Install/configure AutoDarts Desktop or Headless, then run `ochecore boards`. The UI loads
   account boards after login and has a **Refresh boards** button.
4. Select your board in the UI and save, or use its discovered ID with the CLI.
5. Start a match and check `ochecore events --follow` for game events, or
   `ochecore events --raw --follow` for incoming AutoDarts frames.

Your password stays on AutoDarts. Tokens are refreshed and stored atomically in
`data/tokens.json` with owner-only permissions on Linux. On Windows, protect the directory
with your user account permissions. Tokens are plaintext secrets, excluded from Git and the
Docker image. Use one process/worker per data directory. **Disconnect** removes this local
session; it does not revoke other sessions on your account.

## Web interface

Open `http://127.0.0.1:9180` when the service is running.

Both themes use [Oche's](https://github.com/mondeggo/oche) red and cream identity with neutral surfaces.
Use **Dark theme** at the bottom of the sidebar to switch to light mode. The browser remembers
your choice. Content is centered in the space beside the sidebar, with text left-aligned.
The [Oche palette handoff](docs/oche-theme.css) contains matching dark/light CSS variables and
component mapping notes for the Oche app. It is a reference file, not loaded by OcheCore.

- **Overview** shows the selected board, account, cloud connection and game event count.
- **AutoDarts** is a main navigation item for the core account and board connection, separate
  from optional integrations. OAuth settings and connection
  diagnostics are under expandable details.
- **Events** switches between normalized game events and raw AutoDarts frames. Open an entry
  to inspect its JSON payload; the view updates automatically.
- **All integrations** switches integrations on or off immediately. Enabled integrations appear
  below it in the sidebar; disabling them keeps their saved configuration. Changes through the
  CLI or another browser also update navigation.
- **WLED** manages controllers, lighting targets, phase colours, game effects and matrix
  scores. Changes save automatically; named profiles keep different colours and effects.
  Preview buttons sit beside the settings and work with unsaved changes.
- **Caller** selects and installs voices, chooses host/browser output and tests announcements.
  Saving a voice downloads only that pack and removes the old cache once it is ready.
  See the [caller guide](docs/CALLER.md) for terminal controls and game-mode behavior.

The interface uses plain HTML, CSS and JavaScript in `src/ochecore/static/`, without a frontend
build step. Navigation stays in the browser; all controls use the same API as the terminal.
Settings and login data are saved as JSON in `data/`; there is no database.

## WLED lighting and matrix scores

Enable **WLED** in **All integrations**, then open its **Settings** or sidebar entry.
Choose **Discover devices**, then **Add** next to a controller. Discovery shows verified
controllers by name and address. CLI/API discovery also works while automation is disabled.
You can also use **Add device** to enter an HTTP address manually. **Check connection** uses
the address currently in the form, including an unsaved address, and loads segments and effects.
Valid edits save after a short pause; incomplete fields or failed saves keep the draft visible.
The save indicator offers retry after errors. Add a target and choose its segment and mode.
**Preview** beside a phase or game effect uses the current form for three seconds. Matrix
settings have a score preview. Previews also work without a match or enabled automation.

The core restores the previous segment settings and master power after a preview, then
resumes enabled automation. Frozen pixel data cannot be read back through WLED's state API;
previewing a new target on such a segment requires selecting a native effect in WLED first.
Existing targets whose pixels OcheCore controls can be restored from their saved rules.
Colour controls use the device's [effect metadata](https://kno.wled.ge/interfaces/json-api/#effect-metadata)
to hide the primary colour when an effect does not use it. Older firmware keeps the colour
control visible when metadata is unavailable.

### Lighting profiles

Use **Copy current profile** to create a named profile, then adjust its colours and effects.
Selecting a profile applies it immediately. Profiles share device addresses, target geometry,
and automation switches; each stores phase appearances, event effects, player colours and matrix appearance.
Existing installations start with a **Default** profile. Settings persist in `data/wled.json`.
Adding or removing a target updates all profiles; pixel and matrix targets use solid colours.

**Lights off** controls master power and pauses automatic updates for that device. **Lights
on** resumes its saved automation if enabled. Disabling the integration keeps the lights'
waiting appearance; use **Lights off** when you want them dark.

- **Whole segment**: independent colour, brightness and native WLED effect for each phase.
- **LED range / individual LED**: choose a starting pixel and count; count `1` selects one LED.
- **Matrix score**: choose remaining score, visit total or last dart. Set dimensions, rotation
  and row wiring. Corrections and silent reconnection snapshots update the displayed score.

Defaults are green for confirmed ready, yellow for takeout, red for waiting and dim white
when idle. The **Event matrix**, informed by AutoGlow 2's per-segment configuration, groups rules into:

- **Match & victories:** leg and match wins.
- **Hits & throws:** any dart, single, double, triple, outer bull, bullseye, miss, 180 and bust.
- **Game flow & transitions:** match start/end, turn start, takeout start/end, manual reset,
  and calibration start/end.
- **Throw — players:** up to ten ready-to-throw appearances. Empty name filters match the
  player's one-based match position; a name filter matches a case-insensitive substring instead.
  The first enabled matching slot wins. Player colours apply only to local players while ready.

The flow strip opens the relevant settings and highlights the current phase. Phase/player colours
last while that state is active; event effects last for their configured duration (0.1–30 seconds).
Match win has highest priority, followed by leg win, bust, 180, bullseye and other effects.
A specific hit rule takes precedence over **Any dart** within each target. Corrections and new
turns clear temporary effects. Player actions affect local players only; board and match lifecycle
events do not require a player. Snapshots never celebrate. Every target keeps independent rules,
so a white illumination segment can remain steady while an RGB ring reacts to hits.

The core requires an online board, a healthy cloud connection and an explicit `Throw`, `Ready`
or `Ready for throw` status to show green. A score update alone does not establish readiness.
Unknown readiness shows red with a reason on the WLED page and `ochecore game`. These status
names include `Throw` confirmed in the October 2 live capture. Connection loss clears scores
and requests the waiting appearance on reachable lights that have not been turned off.

### Terminal setup

Edit a copy of [the example configuration](config/wled.example.json) with your controller
address and existing segment IDs. It assumes a surround on segment `0` and a 16×8 matrix on
segment `1`; remove the matrix target if you do not have one. Keep the service running, then:

```powershell
uv run ochecore wled discover
uv run ochecore wled config --file config/wled.example.json
uv run ochecore wled probe board
uv run ochecore wled probe --url http://wled.local
uv run ochecore wled enable
uv run ochecore wled test board --target ring --phase takeout
uv run ochecore wled test board --target score --value 180
uv run ochecore wled test board --target ring --event double
uv run ochecore wled test board --target ring --player 1
uv run ochecore wled status
uv run ochecore wled off board
uv run ochecore wled on board
uv run ochecore wled profiles create Quiet
uv run ochecore wled profiles use Default
uv run ochecore wled profiles
uv run ochecore game
uv run ochecore wled disable board
```

`wled enable` / `disable` without a device ID controls the whole integration. To edit settings,
save the output of `ochecore wled config` to a JSON file and reload it with `--file`. Settings
are validated and saved atomically in `data/wled.json`; no restart is required. Per-target
`enabled`, phase appearances and event effects use the same configuration through API and UI.
An empty `effects` object disables temporary effects for a target. Use identical phase colours
and no effects for steady illumination. Standard Docker commands use
`docker compose exec ochecore ochecore wled ...`.

### First-version limits

Configure LED geometry and turn on master power in WLED first; master brightness must be above
zero. Assign dedicated, non-overlapping segments to OcheCore. Individual pixel control freezes
the native effect for its entire segment and sets segment brightness to full, scaling colours
per target. Other pixels in that segment therefore share those settings. Untargeted segments
are left alone. Saved appearances belong to OcheCore; applying native whole-device WLED presets
is not included in this version.

The matrix renderer expects a linear segment with consecutive pixels in rows, up to 512 pixels
per matrix and 1024 individually controlled pixels per controller. Test orientation with a
sample score; WLED grouping, custom maps and 2D segment layouts require hardware verification.
Unknown or oversized numbers show dashes. Player names and scrolling messages are planned.

Each controller has its own bounded event queue and timeout. Offline controllers retry without
blocking AutoDarts or other lights; old effects are discarded. Disabling a controller or removing
its targets requests the waiting appearance and clears matrix scores before stopping.
Ordinary colour/profile changes apply without an intermediate waiting flash.
It does not restore a previous WLED preset. A disconnected controller cannot receive that reset.

`ochecore wled off` / `on` without a device ID controls all saved controllers, including
disabled ones, and reports failures per device. To preview a device JSON definition without
saving, use `ochecore wled test --file device.json --target ring --phase ready`. Add
`--event bust` to preview a game effect. The file contains one device object from the config's
`devices` list. `ochecore wled profiles delete Quiet` removes a profile; keep at least one.

### Device discovery

Discovery runs in the service, on the machine hosting OcheCore. It browses WLED's IPv4 mDNS
service (`_wled._tcp.local.`), then checks each candidate's JSON API without changing its state.
Results contain `name`, `address`, `url`, `hostname` and `version`. The scan takes about three
to six seconds, with bounded concurrent checks and duplicate addresses removed. Only one scan
runs at a time; regular lighting and AutoDarts processing continue.

Controllers need mDNS enabled and must be reachable from OcheCore's network. Multicast discovery
may not cross VLANs, guest Wi-Fi or Docker bridge networks. If no devices appear, use **Add
device** with the controller's IP address. The browser's network is not used for discovery.
The protocol follows [WLED's mDNS advertisement](https://github.com/wled/WLED/blob/main/wled00/wled.cpp)
and uses the Python `zeroconf` library. Discovery currently covers IPv4 advertisements;
subnet address sweeps and automatic device registration are not included.

## Configuration

`config/config.yaml` is loaded at startup. Priority, from highest to lowest:

1. Explicit constructor arguments, used for tests or embedding.
2. Non-empty `OCHECORE_*` environment variables.
3. Non-empty variables in `.env`.
4. `config/config.yaml`.
5. Built-in defaults.

Non-empty connection fields from these sources override saved settings and lock updates
through the API, CLI and UI. Leave them empty to configure through either client; settings are
saved in `data/connection.json`. Tokens are stored separately.

Copy `.env.example` to `.env` for environment-based setup. Restart after changing YAML or
`.env`. Docker fixes the container port to 9180 and data directory to `/data`;
`OCHECORE_PORT` controls the published host port.

The old browser Board Manager on port 3180 is unsupported in AutoDarts v2; local detection
still runs on the board. OcheCore no longer polls the legacy `/api/state` endpoint. Existing
saved `local_board_url` settings are ignored on load and omitted on the next configuration
save. Old YAML/environment options for that adapter are also ignored. See the
[API research](docs/AUTODARTS_API.md) for evidence and scope.

## API

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Process liveness, even before configuration |
| `GET /readyz` | 200 if the cloud stream is connected, otherwise 503 |
| `GET /api/status` | Connection states and counters without secrets |
| `GET /api/game` | Current phase, reason, player and display scores |
| `GET /api/wled`, `PUT /api/wled` | Saved WLED controllers and target configuration |
| `PATCH /api/wled` | Update only supplied top-level fields: `enabled` or the complete `devices` list; omitted fields stay unchanged |
| `GET /api/wled/status` | Device connectivity, errors, capabilities and current phase |
| `POST /api/wled/discover` | Find reachable WLED controllers via mDNS; body `{}`; does not save devices |
| `POST /api/wled/{device_id}/probe` | Check controller capabilities; body `{}` |
| `POST /api/wled/probe` | Check an unsaved address; body `{"url":"http://wled.local"}` |
| `POST /api/wled/preview` | Temporary unsaved test: `device`, `target_id`, optional `phase`, `event`, `player`, `value`, `duration`; responds after restoration |
| `POST /api/wled/{device_id}/power` | Set master power with `{"on":false}` or `{"on":true}`; keeps saved rules |
| `POST /api/wled/power` | Set master power on all saved devices; same body, per-device results |
| `POST /api/wled/profiles` | Copy the current lighting profile; body `{"name":"Quiet"}` |
| `PUT /api/wled/profile` | Select a profile; body `{"id":"default"}` |
| `DELETE /api/wled/profiles/{profile_id}` | Remove a profile; the last profile is retained |
| `POST /api/wled/{device_id}/test` | Timed preview: `target_id`, optional `phase`, `event`, `player`, `value`, `duration` |
| `GET /api/config`, `PUT /api/config` | Public connection settings |
| `GET /api/boards` | Account boards and current selection; requires login |
| `POST /api/auth/login` | Start device authorization |
| `POST /api/auth/logout` | Remove the local session |
| `GET /api/events` | Latest 100 normalized game events |
| `GET /api/events/raw` | Latest 100 incoming AutoDarts frames, preserving their JSON structure |
| `WS /events` | Live normalized game events |
| `WS /events/raw` | Live incoming AutoDarts frames |
| `/docs` | API documentation when UI is enabled; schema always at `/openapi.json` |

POST/PUT requests require `Content-Type: application/json`; use `{}` for login/logout.
WebSockets use native JSON, not Socket.IO. Slow subscribers have bounded queues and lose
oldest messages on overflow; status reports separate delivery-drop counters for each stream.
Histories are held in memory, with no WebSocket replay or delivery acknowledgements.
REST snapshots establish a silent baseline. Corrections and removals have distinct events,
and repeated states do not reannounce darts or wins. See [event rules](docs/EVENTS.md) for
the supported payload contract and deduplication limits.

The interface is for trusted local networks. `OCHECORE_HOST` controls the uv listening address;
`OCHECORE_PUBLISH_HOST` controls Docker's host binding. Standard Docker startup publishes on
`127.0.0.1`; development startup listens on all interfaces as described below.
Anyone who can access it can read events and edit settings. Remote access requires an
authenticated reverse proxy and TLS; do not publish port 9180 directly to the Internet.

## Development

Use `uv run ochecore serve` with the checked-in configuration, or run `scripts\dev.bat` on
Windows / `bash scripts/dev.sh` on Linux and macOS for Docker development with live logs.
The Docker development scripts set `OCHECORE_PUBLISH_HOST=0.0.0.0` before starting Compose,
overriding the `.env` value. An explicit shell environment value takes precedence.
Open `http://YOUR_LAN_IP:9180` from another device, using your configured port if different.

```powershell
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

Tests use HTTP mocks and a local WebSocket test server; no AutoDarts account is required.
[Validation notes](docs/VALIDATION.md) include a Windows temporary-directory workaround.

### GitHub builds

The **Build** workflow runs Ruff and pytest on Python 3.12 and 3.13 for pull requests and
pushes to `main` or `master`. No repository secrets or AutoDarts account are needed.

Build artifacts are created only when a push to `main` or `master` changes
`project.version` in `pyproject.toml` and the checks pass. The comparison covers the whole
push, including multiple commits or a merged pull request. The first push of the project
also creates an initial build. Dependency changes alone and pull requests run checks only.

To create a new build, update the version in `pyproject.toml` and
`src/ochecore/__init__.py`, run `uv lock`, then commit and push those changes. Tags do not
trigger another build. To retry a failed build, rerun its original workflow run in Actions.

A successful version change produces these downloadable artifacts:

| Artifact | Contents |
|---|---|
| `ochecore-python` | Python wheel and source archive |
| `ochecore-docker-amd64` | Docker image for Linux x64 |
| `ochecore-docker-arm64` | Docker image for Linux ARM64, including 64-bit Raspberry Pi OS |

Each package and image is checked for CLI startup, API health, voice catalogue and optional
UI assets. ARM64 uses emulation; testing on a physical Raspberry Pi is still needed. Voice
recordings are downloaded when selected at runtime and are not bundled in builds.

Download artifacts from a completed run's **Artifacts** section; they are retained for 14
days. Extract the download, then install a wheel with `uv tool install path/to/ochecore.whl`
(use the actual filename), or load a Docker archive:

```sh
docker load --input ochecore-linux-amd64.tar.gz
docker run --rm -p 127.0.0.1:9180:9180 --mount type=bind,source="$(pwd)/data",target=/data ochecore:build-RUN_NUMBER-amd64
```

Create `data` first and replace `RUN_NUMBER` with the workflow run number; `docker load`
also prints the full image tag. On Linux, make the folder writable by container UID 10001.
For ARM64, replace `amd64` with `arm64`. These builds are uploaded as Actions artifacts;
the workflow does not publish to PyPI, a container registry or GitHub Releases. The package
version is the version declared in `pyproject.toml`.

Use English in all maintained source, comments, messages, UI, configuration examples and
documentation. `.idea` remains the original user brief; `ressources/` contains unchanged
third-party references. Neither is shipped in the package or Docker image.
