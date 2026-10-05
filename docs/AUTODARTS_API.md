# AutoDarts connection protocol

OcheCore connects to the AutoDarts cloud. Detection runs separately on the board computer;
the service needs no browser except for approving account login on another device.

## Authentication

Use OAuth Device Authorization with an assigned Client ID that has the device grant enabled.
No client secret or account password is stored by OcheCore. The
[OAuth integration guide](https://gist.github.com/lloydowen/960079f2b518f6f5d68e160465298964)
describes client registration. The development configuration uses `darts-caller`; replace it
with OcheCore's assigned ID before distribution.

OAuth and REST requests use `https://api.autodarts.com`. JSON bodies use `client_id`,
`device_code` and `refresh_token`. Refresh tokens rotate and are saved atomically in
`data/tokens.json`. TLS verification is enabled; credentials are not forwarded through redirects,
and upstream error bodies are not exposed to clients.

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/v1/device/code` | Request a code for account approval |
| POST | `/auth/v1/device/token` | Poll for approval |
| POST | `/auth/v1/refresh` | Rotate tokens |
| GET | `/auth/v1/userinfo` | Read account identity |
| GET | `/bs/v0/boards` | Discover account boards |
| GET | `/bs/v0/boards/{boardId}` | Read board status and current match ID |
| GET | `/gs/v0/matches/{matchId}/state` | Load a match snapshot |

Board discovery leaves selection explicit. The CLI and UI both use `/api/boards` and the
configuration API to select a board.

## Cloud stream

Request a ticket with `POST https://play.ws.autodarts.com/ms/v0/tickets` and an
`Authorization: Bearer ...` header. Connect to
`wss://play.ws.autodarts.com/ms/v0/subscribe?code={ticket}` using the response's `code`.
The WebSocket receives no bearer header. Obtain a fresh ticket for every connection attempt;
tickets are neither persisted nor exposed in status responses.

```json
{"type":"subscribe","channel":"autodarts.boards","topic":"BOARD_ID.matches"}
```

| Channel | Topic | Purpose |
|---|---|---|
| `autodarts.boards` | `{boardId}.matches` | Match changes |
| `autodarts.boards` | `{boardId}.events` | Board events |
| `autodarts.boards` | `{boardId}.state` | Board status |
| `autodarts.matches` | `{matchId}.state` | Scoring state |
| `autodarts.matches` | `{matchId}.events` | Match events |

Subscribe to board topics, read the board, subscribe to its match and load a REST snapshot.
Remove the previous match subscription when the match changes. Periodic reconciliation catches
missed lifecycle updates. Reconnecting restores subscriptions and reloads a silent baseline.

Only board and match topics are requested. A rejected subscription appears in
`cloud.subscription_errors`, marks the connection `degraded` and makes `/readyz` return 503.

Connection code lives in `src/ochecore/autodarts/`; normalization and dispatch live in
`src/ochecore/events.py`. See the [event API](EVENTS.md) for raw frames, normalized events and
interpretation limits. OcheCore streams use native JSON WebSockets; legacy Socket.IO clients
require a separate adapter.
