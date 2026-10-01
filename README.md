# OcheCore

Headless AutoDarts event gateway built with Python, uv and Docker.

**Phase 1:** OAuth device login, cloud events, automatic reconnection, terminal controls and
a WebSocket event stream. A small optional web interface uses the same API as the CLI.
The service owns authentication, persistence and event processing and keeps running when
either client closes. It starts without a configured board. Audio, WLED, MQTT, Home Assistant
and gameplay normalization are planned for later phases.

- [Project plan](docs/PLAN.md)
- [API research](docs/AUTODARTS_API.md)
- [Validation notes](docs/VALIDATION.md)

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
│       ├── events.py        # event models, parsing and bounded dispatch
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
The `./data:/data` bind mount contains saved connection settings and OAuth tokens. It is
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
5. Start a match and check the cloud connection and incoming `match.state` events.

Your password stays on AutoDarts. Tokens are refreshed and stored atomically in
`data/tokens.json` with owner-only permissions on Linux. On Windows, protect the directory
with your user account permissions. Tokens are plaintext secrets, excluded from Git and the
Docker image. Use one process/worker per data directory. **Disconnect** removes this local
session; it does not revoke other sessions on your account.

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
| `GET /api/config`, `PUT /api/config` | Public connection settings |
| `GET /api/boards` | Account boards and current selection; requires login |
| `POST /api/auth/login` | Start device authorization |
| `POST /api/auth/logout` | Remove the local session |
| `GET /api/events` | Latest 100 events, held in memory |
| `WS /events` | Live events, without replay or delivery acknowledgements |
| `/docs` | API documentation when UI is enabled; schema always at `/openapi.json` |

POST/PUT requests require `Content-Type: application/json`; use `{}` for login/logout.
The WebSocket uses native JSON, not Socket.IO. Slow subscribers have bounded queues and lose
oldest messages on overflow. Sequence gaps reveal losses; `dropped_deliveries` counts them.
A `snapshot: true` message is initial state, not a new throw. Phase 2 will implement
normalization and deduplication; this stream is not a durable event journal.

The interface is for trusted local use and binds to `127.0.0.1`. For a trusted LAN, set
`OCHECORE_HOST=0.0.0.0` with uv or `OCHECORE_PUBLISH_HOST=0.0.0.0` with Docker.
Anyone who can access it can read events and edit settings. Remote access requires an
authenticated reverse proxy and TLS; do not publish port 9180 directly to the Internet.

## Development

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
