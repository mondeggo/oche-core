# OcheCore

Headless AutoDarts event gateway built with Python, uv and Docker.

OAuth device login, board discovery, automatic reconnection and normalized game events,
with separate raw AutoDarts streams. A small optional web interface uses the same API as the CLI.
The service owns authentication, persistence and event processing and keeps running when
either client closes. It starts without a configured board. WLED supports phase lighting,
temporary game effects and numeric matrix scores. Audio, MQTT and Home Assistant are planned
for later phases. X01 event interpretation is implemented and tested against
the supplied reference contracts; full live gameplay acceptance remains to be done.

- [Project plan](docs/PLAN.md)
- [API research](docs/AUTODARTS_API.md)
- [Validation notes](docs/VALIDATION.md)
- [Event API and normalization rules](docs/EVENTS.md)

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
The `./data:/data` bind mount contains saved connection/WLED settings and OAuth tokens. It is
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
- **AutoDarts** contains account login and board selection. OAuth settings and connection
  diagnostics are under expandable details.
- **Events** switches between normalized game events and raw AutoDarts frames. Open an entry
  to inspect its JSON payload; the view updates automatically.
- **Integrations** links to AutoDarts and WLED. Caller is marked as planned.
- **WLED** manages controllers, lighting targets, phase colours, game effects and matrix
  scores. Saved targets have a timed test button; settings remain active when the page closes.

The interface uses plain HTML, CSS and JavaScript in `src/ochecore/static/`, without a frontend
build step. Navigation stays in the browser; all controls use the same API as the terminal.
Settings and login data are saved as JSON in `data/`; there is no database.

## WLED lighting and matrix scores

Open **WLED → Discover devices**, then choose **Add** next to a controller. Discovery shows
verified controllers by name and address; it also works while WLED automation is disabled.
Adding fills the form without saving or activating any lights. You can also use **Add device**
to enter an HTTP address manually. Save the device and select **Check connection**.
This loads its existing segments and available effects. Add a lighting target, choose its
segment and mode, then enable WLED and save. **Test for 3 seconds** previews the target without
a match and returns to the current game phase afterward.

- **Whole segment**: independent colour, brightness and native WLED effect for each phase.
- **LED range / individual LED**: choose a starting pixel and count; count `1` selects one LED.
- **Matrix score**: choose remaining score, visit total or last dart. Set dimensions, rotation
  and row wiring. Corrections and silent reconnection snapshots update the displayed score.

Defaults are green for confirmed ready, yellow for takeout, red for waiting and dim white
when idle. Each target has optional triple, bull, 180, bust, leg-win and match-win effects.
Match win has highest priority, followed by leg win, bust, 180, bull and triple. Corrections
cancel temporary effects. Only local players trigger effects; snapshots never celebrate.

The core requires an online board, a healthy cloud connection and an explicit `Ready` or
`Ready for throw` status to show green. A score update alone does not establish readiness.
Unknown readiness shows red with a reason on the WLED page and `ochecore game`. These status
names come from the supplied references; they still need checking against live AutoDarts v2
gameplay. Connection loss clears scores and requests the waiting appearance on reachable lights.

### Terminal setup

Edit a copy of [the example configuration](config/wled.example.json) with your controller
address and existing segment IDs. It assumes a surround on segment `0` and a 16×8 matrix on
segment `1`; remove the matrix target if you do not have one. Keep the service running, then:

```powershell
uv run ochecore wled discover
uv run ochecore wled config --file config/wled.example.json
uv run ochecore wled probe board
uv run ochecore wled enable
uv run ochecore wled test board --target ring --phase takeout
uv run ochecore wled test board --target score --value 180
uv run ochecore wled status
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
blocking AutoDarts or other lights; old effects are discarded. Disabling or reconfiguring makes
a best-effort request for the waiting appearance and clears matrix scores before stopping.
It does not restore a previous WLED preset. A disconnected controller cannot receive that reset.

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
| `GET /api/wled/status` | Device connectivity, errors, capabilities and current phase |
| `POST /api/wled/discover` | Find reachable WLED controllers via mDNS; body `{}`; does not save devices |
| `POST /api/wled/{device_id}/probe` | Check controller capabilities; body `{}` |
| `POST /api/wled/{device_id}/test` | Timed preview: `target_id`, `phase`, optional `value`, `duration` |
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

Use English in all maintained source, comments, messages, UI, configuration examples and
documentation. `.idea` remains the original user brief; `ressources/` contains unchanged
third-party references. Neither is shipped in the package or Docker image.
