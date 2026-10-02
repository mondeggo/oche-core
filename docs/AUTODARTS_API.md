# AutoDarts API research and connection contract

Checked October 1, 2026. Supplied projects under `ressources/` are interoperability references.
Live board discovery and the cloud connection have also been checked against the installed board.

## Recent changes

### Headless and local detection

Official documentation describes headless Linux/macOS using the same detection engine as
Desktop. The browser Board Manager on port 3180 is no longer supported starting with v2;
`autodarts` and `autodarts remote` replace it. Local detection still exists, but that does not
guarantee a public local throw API.

Source: [AutoDarts Headless Installation](https://docs.autodarts.com/getting-started/detection/headless-installation/).

The ioBroker adapter reports that `/api/state` may still respond without `throws` or
`numThrows` on v2 and recommends cloud mode for throws. This is community evidence.
Its older password-login example is not the OAuth contract selected for OcheCore.

Source: [inventwo/ioBroker.autodarts](https://github.com/inventwo/ioBroker.autodarts).

Decision: use the cloud connection and remove the legacy HTTP polling adapter from OcheCore.
The documented removal concerns the old Board Manager; local detection itself still runs on
the board. We have not established a supported v2 local throw API. OcheCore being headless
means its controls need no local browser; its event connection still requires the cloud.

### Domains and OAuth

Tools for AutoDarts documents Keycloak shutdown on June 28, 2026 and the later August move
from `.io` to `.com`. Its supplied source now uses `api.autodarts.com`.

Sources: [Tools changelog](https://github.com/creazy231/tools-for-autodarts/blob/main/CHANGELOG.md)
and the supplied `entrypoints/auth-cookie.ts`.

The third-party guide describes Device Authorization with an assigned Client ID and explicit
grant enablement, JSON requests and rotating refresh tokens. Its examples still use `.io`;
OcheCore uses the current `.com` origin.

Source: [AutoDarts OAuth migration guide](https://gist.github.com/lloydowen/960079f2b518f6f5d68e160465298964).

The [current discovery document](https://api.autodarts.com/.well-known/openid-configuration)
was fetched directly from this machine. Observed values:

| Field | Value |
|---|---|
| issuer | `https://api.autodarts.com/auth` |
| device_authorization_endpoint | `https://api.autodarts.com/auth/v1/device/code` |
| token_endpoint | `https://api.autodarts.com/auth/v1/exchange` |
| userinfo_endpoint | `https://api.autodarts.com/auth/v1/userinfo` |
| authorization_endpoint | `https://api.autodarts.com/auth/v1/oauth/authorize` |

Discovery lists extra grants including `password`, while the third-party guide does not
authorize that flow for this integration. OcheCore implements Device Authorization.
The discovery exchange endpoint is not used for device polling or refresh.

## Implemented requests

REST and OAuth origin: `https://api.autodarts.com`. The messaging gateway uses
`https://play.ws.autodarts.com`. TLS verification is enabled; redirects are not followed
with credentials. Upstream error bodies are not exposed in the interface.

| Method | Path | Purpose and evidence |
|---|---|---|
| POST | `/auth/v1/device/code` | Client ID to code; guide and live discovery |
| POST | `/auth/v1/device/token` | Approval polling; integration guide |
| POST | `/auth/v1/refresh` | Token rotation; integration guide |
| GET | `/auth/v1/userinfo` | User identity; guide and discovery |
| GET | `/bs/v0/boards` | Account board discovery; supplied AutoGlow-2 source |
| GET | `/bs/v0/boards/{boardId}` | Board and matchId; supplied sources |
| GET | `/gs/v0/matches/{matchId}/state` | Detailed snapshot; current Tools/ATA sources |
| POST | `https://play.ws.autodarts.com/ms/v0/tickets` | Bearer token to connection ticket; AutoGlow and current AutoDarts Play client |
| WS | `wss://play.ws.autodarts.com/ms/v0/subscribe?code={ticket}` | Cloud stream authenticated by ticket |

OAuth bodies use `client_id`, `device_code` and `refresh_token` from the integration guide.
The ATA browser catalogue also shows `refreshToken`; this is not the selected Device Grant
contract. Confirm the device-client flow during real acceptance.

## Cloud subscriptions

Request a fresh ticket with `Authorization: Bearer …`, then connect to the messaging
gateway with its `code` in the query string. The WebSocket itself receives no bearer
header. Obtain a new ticket on each connection attempt and after token renewal; tickets
are not persisted or exposed in status responses. Example subscription:

```json
{"type":"subscribe","channel":"autodarts.boards","topic":"BOARD_ID.matches"}
```

| Channel | Topic | OcheCore event |
|---|---|---|
| `autodarts.boards` | `{boardId}.matches` | `board.matches` |
| `autodarts.boards` | `{boardId}.events` | `board.events` |
| `autodarts.boards` | `{boardId}.state` | `board.state` |
| `autodarts.matches` | `{matchId}.state` | `match.state` |
| `autodarts.matches` | `{matchId}.events` | `match.events` |

Subscribe to board topics, read the board, subscribe to its match, and load a REST snapshot.
Remove the previous subscription when the match changes. Reconcile periodically to catch
missed lifecycle events. Restore subscriptions and reload state after reconnecting.

The October 2 live capture confirmed that the development `darts-caller` Client ID rejects
`autodarts.users` with `unauthorized client`. OcheCore no longer requests that unused topic
or loads the user profile during stream setup. Board and match topics provide game events.
Rejected board or match subscriptions still mark the connection `degraded`; readiness returns 503.

References: supplied caller functions `on_open_autodarts`, `listen_to_match` and
`on_message_autodarts`; Tools `entrypoints/match.content/index.ts`; and the
[ATA API catalogue](https://github.com/thomasasen/autodarts_local_tournament/blob/main/docs/autodarts-api-capabilities.md).

OcheCore keeps connection and match tracking in `src/ochecore/autodarts/cloud.py`,
with event validation, normalization and dispatch in `src/ochecore/events.py`. The raw API
retains incoming WebSocket frames; the normalized API exposes interpreted game events.
See [event contracts](EVENTS.md) for reference evidence and current coverage.

## Supplied references

| Project | Relevant role |
|---|---|
| Legacy darts-caller | Cloud frames, match lifecycle and announcement vocabulary |
| Current darts-caller (Peschi90) | Device login for headless; binary distribution |
| AutoGlow-2 | Device login, WebSocket ticket handshake, board discovery and event interpretation |
| Tools for AutoDarts | Current domains/auth, match state and local-player identification |
| autodarts_local_tournament | Recent REST catalogue and coverage limits |
| autodarts-xconfig | Reference for future settings and interface organization |
| darts-wled | Effects, presets and caller Socket.IO connection |
| darts-gif / darts-pixelit | Caller event consumers for displays and hardware |
| darts-extern | Caller gateway to other platforms |
| darts-voice | Local voice commands; possible later integration |

The [maintained caller](https://github.com/Peschi90/darts-caller) confirms device login but retains
historical email/password instructions beside its OAuth section. OcheCore follows the OAuth
guide. Existing extensions use **Socket.IO** on port 8079; they are not directly compatible
with OcheCore's native JSON WebSocket at `/events`.

## AutoGlow-2 review — October 1, 2026

The supplied [device routes](../ressources/AutoGlow-2/routes/autodarts_routes.py) implement
the same JSON device-code flow as OcheCore. They hardcode `DEVICE_CLIENT_ID = "darts-caller"`,
which explains why AutoGlow's UI does not require a Client ID. Its password-login path
separately uses `autodarts-play`. These are application identifiers, not evidence that an
OcheCore client has been registered. The OAuth guide still calls for an assigned Client ID
with the device grant enabled.

The [connection client](../ressources/AutoGlow-2/core/autodarts_client.py) adds a crucial
handshake: POST `/ms/v0/tickets` on the messaging gateway, read the response's `code`, then
connect to `/ms/v0/subscribe?code=...`. This is also present in the
[AutoDarts Play client bundle inspected on this date](https://play.autodarts.com/assets/clients-CqbIjROi.js).
OcheCore now follows that flow, replacing its previous assumption of a direct bearer
WebSocket on the REST API host. Its tests check ticket renewal on reconnect, 401 refresh,
URL encoding and malformed/error responses. The running service has now discovered an online
board using the saved account session, connected the cloud stream and received initial match events.

Other useful references for later work:

- `GET /bs/v0/boards` returns the account's boards in AutoGlow's implementation. OcheCore
  now exposes discovery through `/api/boards`, the `boards` CLI command and the UI selector.
  The response includes only ID, name and online status; it does not select a board.
- AutoGlow subscribes to board `state`, `events` and `matches`, plus match `state` and
  `events`. OcheCore now subscribes to all these topics and preserves them in the raw stream.
- Its refresh helper tries three endpoints and two Client IDs. OcheCore retains the
  documented `/auth/v1/refresh` request with the same Client ID used for login.
- Its AutoDarts adapter uses cloud services. Links to a board's local manager in the UI
  do not implement a local event connection or establish v2 local throw availability.

## Evidence and remaining checks

- Verified online: headless v2 documentation, domain migration, current OIDC discovery
  and the ticket handshake in the current AutoDarts Play JavaScript client.
- Checked in source: REST paths, cloud topics, match lifecycle and legacy Socket.IO.
- Verified in simulation: OcheCore implementation against those contracts.
- Verified live with the development Client ID: saved account access, board discovery and
  selection, cloud connection and initial match events.
- Remaining: OcheCore's assigned Client ID, gameplay payload coverage, long-running refresh
  and full-match recovery.
