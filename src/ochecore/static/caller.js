import { $, api, poll } from "./client.mjs";
import { page, notice, setIntegrationEnabled } from "./app.js";

/* The service selects calls. This optional player only plays the supplied local clips. */
(() => {
  let voices = [],
    config = null,
    current = null,
    dirty = false,
    working = false,
    polling = null;
  let voiceChanged = false,
    settingsTimer = null,
    settingsSave = null,
    settingsError = "",
    catalogueSignature = "",
    catalogueVersion = "",
    stateRevision = 0;
  const pendingSettings = {};
  let context = null,
    socket = null,
    source = null,
    audioQueue = [],
    playing = false,
    generation = 0;
  const flags = [
    "turn_totals",
    "checkouts",
    "players",
    "include_bots",
    "local_only",
  ];
  const languages = new Intl.DisplayNames(["en"], { type: "language" });

  function error(message = "") {
    for (const id of ["caller-error", "caller-integration-error"]) {
      $(id).textContent = message;
      $(id).hidden = !message;
    }
  }

  function options(selected) {
    const language = $("caller-language").value;
    const select = $("caller-voice");
    select.replaceChildren(new Option("Choose a voice", ""));
    voices
      .filter((v) => v.language === language)
      .forEach((v) => {
        select.add(
          new Option(
            `${v.name} · ${v.provider}${v.installed ? " · Installed" : ""}`,
            v.id,
          ),
        );
      });
    select.value = selected || "";
    voiceState();
  }

  function voiceState() {
    $("caller-language").disabled = $("caller-voice").disabled = working;
    const voice = voices.find((v) => v.id === $("caller-voice").value);
    const download = current?.download;
    const installing = ["downloading", "installing"].includes(download?.state);
    $("caller-retry").hidden =
      !current?.voice ||
      installing ||
      (current.installed && download?.state !== "error");
    $("caller-retry").disabled = working || dirty;
    $("caller-preview").hidden = !voice;
    if (voice) $("caller-preview").href = voice.preview_url;
    $("caller-download").textContent = installing
      ? `${download.state === "installing" ? "Installing" : "Downloading"} voice… ${Math.round(download.bytes / 1048576)} MiB${download.total ? ` / ${Math.round(download.total / 1048576)} MiB` : ""}`
      : download?.error ||
        (voice?.installed
          ? "Installed locally. Ready to use."
          : "Choose Download and use voice. The previous pack is removed when the new one is ready.");
    $("caller-save").disabled =
      working ||
      !voiceChanged ||
      !config ||
      !voice ||
      (installing && voice.id !== current.voice);
    $("caller-save").textContent = voice?.installed
      ? "Use voice"
      : "Download and use voice";
    $("caller-unsaved").hidden = !voiceChanged;
    $("caller-autosave").textContent =
      settingsError ||
      (settingsSave
        ? "Saving settings…"
        : Object.keys(pendingSettings).length
          ? "Waiting to save settings…"
          : "Volume, output and announcements save automatically.");
    $("caller-autosave").classList.toggle("error", !!settingsError);
    $("caller-settings-retry").hidden = !settingsError;
    $("caller-test").disabled =
      working || !current?.enabled || !current?.installed || dirty;
  }

  function fill(value) {
    config = value;
    if (dirty) return;
    const signature = JSON.stringify([voices, value]);
    if (signature === catalogueSignature) return;
    catalogueSignature = signature;
    const voice = voices.find((v) => v.id === value.voice);
    const previous = $("caller-language").value;
    $("caller-language").replaceChildren();
    [...new Set(voices.map((v) => v.language))].sort().forEach((code) => {
      $("caller-language").add(new Option(languages.of(code), code));
    });
    $("caller-language").value = voice?.language || previous || "en-GB";
    options(value.voice);
    $("caller-volume").value = Math.round(value.volume * 100);
    $("caller-output").value = value.output;
    $("caller-darts").value = value.darts;
    flags.forEach((key) => {
      $("caller-" + key).checked = value[key];
    });
  }

  function saveSettings() {
    if (working || settingsSave || !Object.keys(pendingSettings).length) return;
    if (!$("caller-form").checkValidity()) {
      settingsError = "Enter a volume from 0 to 100 to save your settings.";
      voiceState();
      return;
    }
    const values = { ...pendingSettings };
    settingsSave = (async () => {
      try {
        await api("/api/caller", "PATCH", values);
        stateRevision += 1;
        Object.assign(config, values);
        for (const [key, value] of Object.entries(values)) {
          if (pendingSettings[key] === value) delete pendingSettings[key];
        }
        settingsError = "";
      } catch (cause) {
        settingsError = `Not saved: ${cause.message}`;
      } finally {
        settingsSave = null;
        dirty = voiceChanged || !!Object.keys(pendingSettings).length;
        voiceState();
        if (Object.keys(pendingSettings).length && !settingsError) {
          settingsTimer = setTimeout(saveSettings, 500);
        }
      }
    })();
    voiceState();
    return settingsSave;
  }

  function stopAudio() {
    generation++;
    audioQueue = [];
    if (source) {
      source.stop();
      source = null;
    }
  }

  async function playQueue() {
    if (playing || !context || context.state !== "running") return;
    playing = true;
    try {
      while (audioQueue.length) {
        const item = audioQueue.shift(),
          version = generation;
        if (item.expires_at * 1000 < Date.now()) continue;
        for (const clip of item.clips) {
          if (version !== generation || item.expires_at * 1000 < Date.now())
            break;
          const response = await fetch(clip.url, {
            signal: AbortSignal.timeout(10000),
          });
          if (!response.ok)
            throw new Error(
              "Cannot load a voice clip. Check the installed pack.",
            );
          const buffer = await context.decodeAudioData(
            await response.arrayBuffer(),
          );
          if (version !== generation) break;
          const node = context.createBufferSource(),
            gain = context.createGain();
          source = node;
          node.buffer = buffer;
          gain.gain.value = item.volume;
          node.connect(gain).connect(context.destination);
          await new Promise((resolve) => {
            node.onended = resolve;
            node.start();
            node.stop(context.currentTime + Math.min(buffer.duration, 12));
          });
          if (source === node) source = null;
        }
      }
    } catch (cause) {
      stopAudio();
      $("caller-browser-state").textContent = cause.message;
    } finally {
      playing = false;
    }
  }

  function syncPlayer() {
    const wanted =
      context &&
      current?.enabled &&
      ["browser", "both"].includes(current?.output);
    $("caller-browser").hidden = !["browser", "both"].includes(current?.output);
    if (!wanted) {
      stopAudio();
      if (socket) {
        const old = socket;
        socket = null;
        old.close();
      }
      return;
    }
    if (socket) return;
    const ws = new WebSocket(
      `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/caller/audio`,
    );
    socket = ws;
    ws.onopen = () => {
      $("caller-browser-state").textContent = "Sound enabled on this browser";
    };
    ws.onmessage = (event) => {
      const item = JSON.parse(event.data);
      if (item.type === "stop") return stopAudio();
      if (item.type !== "play") return;
      if (audioQueue.length >= 16) audioQueue.shift();
      audioQueue.push(item);
      playQueue();
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      stopAudio();
      $("caller-browser-state").textContent =
        "Sound disconnected. Reconnecting…";
    };
  }

  async function refreshCaller() {
    if (working || polling) return polling;
    const revision = stateRevision;
    polling = (async () => {
      try {
        current = await api("/api/caller/status");
        setIntegrationEnabled("caller", current.enabled);
        $("caller-toggle").disabled = false;
        $("caller-toggle").setAttribute(
          "aria-checked",
          String(current.enabled),
        );
        $("caller-toggle-label").textContent = current.enabled ? "On" : "Off";
        $("caller-settings").hidden = !current.enabled;
        $("caller-state").textContent = ["downloading", "installing"].includes(
          current.download.state,
        )
          ? "Preparing voice…"
          : current.error
            ? "Needs attention"
            : current.installed
              ? "Ready"
              : current.voice
                ? "Voice unavailable"
                : "Choose a voice";
        $("overview-caller").textContent = !current.enabled
          ? "Disabled"
          : $("caller-state").textContent;
        error(current.error || "");
        if (page === "caller") {
          const version = `${current.voice}:${current.installed}:${current.download.state}`;
          const [catalogue, settings] = await Promise.all([
            catalogueVersion !== version ? api("/api/caller/voices") : voices,
            api("/api/caller"),
          ]);
          voices = catalogue;
          catalogueVersion = version;
          if (revision === stateRevision) fill(settings);
        }
        voiceState();
        $("caller-test").disabled =
          !current.enabled || !current.installed || dirty;
        const last = current.recent_calls.at(-1);
        $("caller-recent").textContent = current.missing_sounds.length
          ? `Not in this pack: ${current.missing_sounds.join(", ")}`
          : last
            ? `Last call: ${last.sounds.join(" · ")}`
            : "No calls yet.";
        syncPlayer();
      } catch (cause) {
        error(cause.message || "Cannot reach Caller.");
        $("caller-toggle").disabled =
          $("caller-test").disabled =
          $("caller-save").disabled =
            true;
        $("overview-caller").textContent = "Unavailable";
      }
    })().finally(() => {
      polling = null;
    });
    return polling;
  }

  async function change(work) {
    if (working) return;
    working = true;
    clearTimeout(settingsTimer);
    $("caller-toggle").disabled = true;
    voiceState();
    try {
      await polling;
      await settingsSave;
      await work();
    } catch (cause) {
      notice(cause.message, true);
    } finally {
      working = false;
      await refreshCaller();
      if (Object.keys(pendingSettings).length && !settingsError)
        settingsTimer = setTimeout(saveSettings, 500);
    }
  }

  $("caller-toggle").addEventListener("click", () =>
    change(async () => {
      const enabled = !current.enabled;
      await api("/api/caller", "PATCH", { enabled });
      setIntegrationEnabled("caller", enabled);
      if (enabled) location.hash = "caller";
    }),
  );
  $("caller-language").addEventListener("change", () => options(""));
  function editSettings(event) {
    stateRevision += 1;
    const key = event.target.id.replace("caller-", "");
    if (["language", "voice"].includes(key)) {
      voiceChanged = $("caller-voice").value !== config?.voice;
    } else if (["volume", "output", "darts", ...flags].includes(key)) {
      pendingSettings[key] =
        key === "volume"
          ? Number(event.target.value) / 100
          : flags.includes(key)
            ? event.target.checked
            : event.target.value;
      settingsError = "";
      clearTimeout(settingsTimer);
      settingsTimer = setTimeout(saveSettings, 500);
    } else return;
    dirty = voiceChanged || !!Object.keys(pendingSettings).length;
    voiceState();
  }
  $("caller-form").addEventListener("input", editSettings);
  $("caller-form").addEventListener("change", editSettings);
  $("caller-settings-retry").addEventListener("click", saveSettings);
  $("caller-form").addEventListener("submit", (event) => {
    event.preventDefault();
    change(async () => {
      const values = { voice: $("caller-voice").value };
      await api("/api/caller", "PATCH", values);
      stateRevision += 1;
      voiceChanged = false;
      dirty = !!Object.keys(pendingSettings).length;
      notice(
        "Voice selected. Download progress appears below the voice selector.",
      );
    });
  });
  $("caller-retry").addEventListener("click", () =>
    change(async () => {
      await api(
        `/api/caller/voices/${encodeURIComponent(current.voice)}/install`,
        "POST",
      );
    }),
  );
  $("caller-browser").addEventListener("click", async () => {
    try {
      context ||= new AudioContext();
      await context.resume();
      syncPlayer();
    } catch {
      $("caller-browser-state").textContent =
        "This browser could not enable audio.";
    }
  });
  $("caller-test").addEventListener("click", () =>
    change(async () => {
      const call = $("caller-test-call").value;
      await api("/api/caller/test", "POST", {
        call,
        score: call === "checkout" ? 40 : 180,
      });
    }),
  );
  $("caller-stop").addEventListener("click", () =>
    change(async () => {
      stopAudio();
      await api("/api/caller/stop", "POST");
    }),
  );
  window.addEventListener("hashchange", refreshCaller);
  window.addEventListener("pagehide", () => {
    stopAudio();
    socket?.close();
  });
  window.addEventListener("beforeunload", (event) => {
    if (Object.keys(pendingSettings).length || settingsSave) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  poll(refreshCaller, { background: true });
})();
