# Caller

The headless service turns normalized game events into ordered audio calls. The UI and CLI
share the same settings and controls. Enable **Caller** in **All integrations**, then open
its sidebar page, choose a language and voice, and save your settings.
Use **Play test** to check the selected output without starting a match.

## Voices and output

Choose a voice using the [Peschi previews](https://darts-caller-preview.peschi.org/).
French includes Rémi and Léa.
The catalogue is metadata, not downloaded audio. Saving a voice downloads only that selected
pack in the background. Once installation succeeds, other cached packs are removed, along with
temporary ZIP files. A failed download preserves the previous pack; reselect it or use **Retry
download**. Calls pause while the selected voice is being prepared. Wait for the current download
to finish before switching again. A restart resumes installation of a saved selection if needed.

Settings are in `data/caller.json`;
installed clips and their sound-key index are in `data/voices/`. Both persist in the Docker
bind mount. Installed packs work offline. Audio files are not bundled in the repository or image.

- **Host** (default): plays on the service computer's default sound device using SDL. No browser
  is needed. Select the desired default device in the host OS before starting OcheCore.
- **Browser**: open the UI, select this output, save, then press **Enable sound here**. Keep the
  page open; playback continues when navigating between OcheCore pages. Each enabled browser
  receives the calls. Reloading the page requires enabling sound again.
- **Both**: sends calls to host speakers and enabled browsers. Outputs are not synchronized.

Volume, dart announcements, visit totals, checkout reminders, player names, bots and local-only
filtering are configurable. Disabling Caller keeps its settings and selected pack. **Stop sound**
cancels current and queued calls; later game events can play again.

Missing clips are skipped and listed in status. Names fall back to numbered player clips when
available. A pack may lack some names, large scores or special calls. The caller does not invent
a score or concatenate numbers using English grammar for a non-English voice.

## Terminal

With the service running:

```sh
uv run ochecore caller voices --language fr-FR
uv run ochecore caller config --voice amazon-fr-fr-remi-male --output host --volume 0.6
uv run ochecore caller status
# Wait until installed is true, then enable playback:
uv run ochecore caller enable
uv run ochecore caller test --score 180
uv run ochecore caller test --call checkout --score 40
uv run ochecore caller stop
uv run ochecore caller disable
```

`caller config --file caller.json` replaces the complete settings. Individual flags update only
the supplied fields. `caller install VOICE_ID` also selects that voice and starts its download;
it does not build a collection of unused packs. `caller config --voice=` clears the selection
and managed voice cache. A settings file can also set `darts` (`auto`, `segment`, `score`, `off`),
`turn_totals`, `checkouts`, `players`, `include_bots` and `local_only`. Booleans default to true
except `enabled` and `local_only`. Default output is `host`, volume `0.6`, darts `auto`.

## Game modes

All modes share authoritative AutoDarts bust/win handling, player calls and duplicate suppression.
Automatic dart announcements use the following policy; `segment`, `score` and `off` override it.

| Mode | Automatic announcements |
|---|---|
| X01, Random Checkout, 121 | Visit points; checkout reminders when AutoDarts provides a checkout guide |
| Count Up (`CountUp` and `Count-Up` accepted) | Visit points |
| Cricket / Tactics | Dart segments; the visit's `score` field for the running total |
| ATC | Dart segments; next target when provided in match state |
| RTW | Dart segments, visit points and next target when provided |
| Segment Training | Dart segments and provided target |
| Bob's 27 | Dart segments and nonnegative visit points |
| Shanghai | Visit points |
| Gotcha | Visit points; legal checkout reminders toward `settings.targetScore` |
| Bermuda | Visit points, with a minus call for negative points |
| Killer | Dart segments and wins |
| Bull-off | Dart segments, bull-off prompt on a new visit and wins |

ATC, Killer, Segment Training and Bull-off do not announce a physical dart sum as a game total.
Target and checkout fields are optional; absence suppresses that contextual call. These rules
have synthetic replay coverage for all 14 modes. They do not
reimplement the game's scoring engine. Live acceptance across all modes remains pending.

Wins take priority over busts, which take priority over visit totals in the same update batch.
Snapshots and reconnect baselines stay silent. Corrections, undo, editing and disabling clear
queued sound. Queues are bounded, calls expire after eight seconds, and individual clips stop
after twelve seconds. A new browser connection receives future calls only.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/caller` | Saved settings |
| `PUT /api/caller` | Replace settings |
| `PATCH /api/caller` | Update supplied settings |
| `GET /api/caller/status` | Enabled state, output, errors, downloads, missing sounds and recent calls |
| `GET /api/caller/voices` | Catalogue with installed flags and preview links |
| `POST /api/caller/voices/{id}/install` | Select the voice and start its installation; returns 202 |
| `POST /api/caller/test` | Play a sample, e.g. `{"call":"score","score":180}` |
| `POST /api/caller/stop` | Cancel current and queued sound |
| `GET /api/caller/audio/{id}/{clip}` | Serve a local installed clip |
| WebSocket `/caller/audio` | Future browser playback instructions and stop messages |

The audio socket emits `type: "play"`, ordered `clips` (key, file, local URL), volume and an
`expires_at` Unix timestamp, or `type: "stop"`. Clients must serialize clips, drop expired calls
and stop queued/current audio on stop or disconnect. Routes remain available with `--no-ui`.

## Docker audio

Browser output needs no container sound device. On Docker Desktop, use browser output or run
the service natively with uv for host speakers. Linux host output requires an accessible ALSA
device. An optional local Compose override can add:

```yaml
services:
  ochecore:
    devices:
      - /dev/snd:/dev/snd
    group_add:
      - "${AUDIO_GID}"
    environment:
      SDL_AUDIODRIVER: alsa
```

Set `AUDIO_GID` to the numeric group owning `/dev/snd` on that host. This is optional so the
default Compose file also works on Windows. The image includes ALSA/PulseAudio runtime libraries;
physical output still depends on host permissions and sound configuration.

## Voice packs

The catalogue and ZIP/CSV format follow [darts-caller](https://github.com/Peschi90/darts-caller). CSV rows
correspond to sorted audio files; aliases and `+N` variants share a sound key. Nested audio ZIPs
are supported. Installation uses bounded downloads/expansion, generated local filenames and
an atomic directory rename. Only catalogue download URLs can be requested.

The implementation was written independently. Provider audio reuse terms have not been
established. OcheCore downloads selected packs locally and does not redistribute recordings.
