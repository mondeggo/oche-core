/* WLED forms edit service configuration; device traffic stays in the headless core. */
(() => {
  let configuration = null;
  let selected = "";
  let changed = false;
  let working = false;
  let refreshing = null;
  let latestStatus = null;
  let discovered = [];
  const phases = {
    ready: "Ready to throw",
    takeout: "Remove darts",
    waiting: "Wait",
    idle: "Idle",
  };
  const effects = {
    triple: "Triple",
    bull: "Bull",
    score_180: "180",
    bust: "Bust",
    leg_win: "Leg win",
    match_win: "Match win",
  };
  const form = $("wled-form");
  const field = (root, name) => root.querySelector(`[data-field="${name}"]`);
  const matrixField = (root, name) => root.querySelector(`[data-matrix-field="${name}"]`);
  const device = () => configuration?.devices.find((item) => item.id === selected);
  const identity = (prefix) => `${prefix}-${Date.now().toString(36)}`;
  const appearance = (color = "#ffffff") => ({ color, brightness: 128, effect: 0 });
  const newTarget = () => ({
    id: identity("target"),
    name: "Lighting target",
    enabled: true,
    segment: 0,
    mode: "segment",
    start: 0,
    count: 1,
    phases: {
      ready: appearance("#00ff00"),
      takeout: appearance("#ffff00"),
      waiting: appearance("#ff0000"),
      idle: appearance("#202020"),
    },
    effects: {
      score_180: { ...appearance("#ffb000"), duration: 2 },
      bust: { ...appearance("#ff0000"), duration: 2 },
      leg_win: { ...appearance("#00aaff"), duration: 2 },
      match_win: { ...appearance("#a000ff"), duration: 4 },
    },
    matrix: {
      width: 16,
      height: 8,
      serpentine: true,
      rotation: 0,
      source: "remaining",
      appearance: appearance(),
    },
  });

  function buttons() {
    if (working) {
      [...form.elements].forEach((control) => {
        control.disabled = true;
      });
      return;
    }
    $("wled-save").disabled = working || !configuration || !changed;
    $("wled-unsaved").hidden = !changed;
    $("wled-device").disabled = working || changed;
    $("wled-reload").disabled = working;
    $("wled-probe").disabled = working || changed || !device();
    form.querySelectorAll("[data-test]").forEach((button) => {
      const root = button.closest("[data-target]");
      button.disabled =
        working ||
        changed ||
        !configuration?.enabled ||
        !device()?.enabled ||
        !field(root, "enabled").checked;
    });
    $("wled-add-device").disabled = working || !configuration;
    $("wled-discover").disabled = !configuration;
    $("wled-add-device").disabled ||= configuration?.devices.length >= 8;
    form.querySelectorAll("[data-add-found]").forEach((button) => {
      const added = alreadyConfigured(discovered[Number(button.dataset.addFound)]);
      button.disabled = added || configuration.devices.length >= 8;
      button.textContent = added ? "Added" : "Add";
    });
    $("wled-name").disabled = $("wled-url").disabled = !device();
  }

  function markChanged() {
    changed = true;
    buttons();
  }

  function origin(url) {
    try {
      return new URL(url).origin;
    } catch {
      return url;
    }
  }

  function alreadyConfigured(found) {
    const address = new URL(found.url);
    const origins = [address.origin];
    if (found.hostname) {
      address.hostname = found.hostname;
      origins.push(address.origin);
    }
    return configuration.devices.some((item) => origins.includes(origin(item.url)));
  }

  function addDevice(name, url) {
    if (!configuration || configuration.devices.length >= 8) return;
    collect();
    const added = { id: identity("device"), name, url, enabled: true, targets: [] };
    configuration.devices.push(added);
    selected = added.id;
    markChanged();
    render();
  }

  function renderDiscovery() {
    const list = $("wled-discovered");
    list.replaceChildren();
    list.hidden = !discovered.length;
    discovered.forEach((found, index) => {
      const row = document.createElement("div");
      row.className = "wled-discovered-device";
      const description = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = found.name;
      const details = document.createElement("p");
      details.className = "hint";
      details.textContent = `${found.url} · WLED ${found.version}`;
      description.append(title, details);
      const add = document.createElement("button");
      add.type = "button";
      add.dataset.addFound = index;
      add.textContent = "Add";
      add.addEventListener("click", () => {
        collect();
        if (alreadyConfigured(found)) {
          buttons();
          return;
        }
        addDevice(found.name, found.url);
        notice("Device added to the form. Choose its lighting targets, then save settings.");
      });
      row.append(description, add);
      list.append(row);
    });
    buttons();
  }

  function label(text, input) {
    const element = document.createElement("label");
    element.append(text, input);
    return element;
  }

  function input(type, value, name, min, max) {
    const element = document.createElement("input");
    element.type = type;
    element.dataset.value = name;
    if (type === "checkbox") element.checked = value;
    else element.value = value;
    if (min !== undefined) {
      element.min = min;
      element.max = max;
      element.required = true;
    }
    return element;
  }

  function appearanceRow(key, title, value, isEffect, mode) {
    const row = document.createElement("div");
    row.className = "wled-appearance";
    row.dataset.rule = key;
    const name = document.createElement("span");
    name.textContent = title;
    if (isEffect) row.append(label(title, input("checkbox", Boolean(value), "enabled")));
    else row.append(name);
    const settings = value || { ...appearance(), duration: 2 };
    row.append(label("Colour", input("color", settings.color, "color")));
    row.append(label("Brightness", input("number", settings.brightness, "brightness", 0, 255)));
    if (isEffect) {
      const duration = input("number", settings.duration, "duration", 0.1, 30);
      duration.step = "0.1";
      row.append(label("Seconds", duration));
    }
    const select = document.createElement("select");
    select.dataset.value = "effect";
    const info = latestStatus?.devices.find((item) => item.id === selected)?.info;
    (info?.effects || ["Solid"]).forEach((name, index) => {
      if (!["-", "RSVD"].includes(name)) select.add(new Option(name, index));
    });
    if (![...select.options].some((item) => Number(item.value) === settings.effect)) {
      select.add(new Option(`Effect ${settings.effect}`, settings.effect));
    }
    select.value = settings.effect;
    const effectLabel = label("WLED effect", select);
    effectLabel.dataset.nativeEffect = "";
    effectLabel.hidden = mode !== "segment";
    row.append(effectLabel);
    return row;
  }

  function targetMode(root) {
    const mode = field(root, "mode").value;
    root.querySelector("[data-pixels]").hidden = mode === "segment";
    root.querySelector("[data-count]").hidden = mode !== "pixels";
    root.querySelector("[data-matrix]").hidden = mode !== "matrix";
    root.querySelector("[data-phases]").hidden = mode === "matrix";
    root.querySelector("[data-test-score]").hidden = mode !== "matrix";
    root.querySelectorAll("[data-native-effect]").forEach((node) => {
      node.hidden = mode !== "segment";
    });
  }

  function render() {
    $("wled-enabled").checked = configuration.enabled;
    const select = $("wled-device");
    select.replaceChildren(...configuration.devices.map((item) => new Option(item.name, item.id)));
    if (!device()) selected = configuration.devices[0]?.id || "";
    if (!selected) select.add(new Option("No devices configured", ""));
    select.value = selected;
    const current = device();
    $("wled-device-editor").hidden = !current;
    $("wled-name").disabled = $("wled-url").disabled = !current;
    $("wled-targets").replaceChildren();
    if (!current) {
      buttons();
      return;
    }
    $("wled-name").value = current.name;
    $("wled-url").value = current.url;
    $("wled-device-enabled").checked = current.enabled;
    current.targets.forEach((target) => {
      const root = $("wled-target-template").content.firstElementChild.cloneNode(true);
      root.dataset.target = target.id;
      root.querySelector("[data-title]").textContent = target.name;
      ["name", "mode", "segment", "start", "count"].forEach((key) => {
        field(root, key).value = target[key];
      });
      field(root, "enabled").checked = target.enabled;
      ["source", "rotation", "width", "height"].forEach((key) => {
        matrixField(root, key).value = target.matrix[key];
      });
      ["color", "brightness"].forEach((key) => {
        matrixField(root, key).value = target.matrix.appearance[key];
      });
      matrixField(root, "serpentine").checked = target.matrix.serpentine;
      for (const [key, title] of Object.entries(phases))
        root
          .querySelector("[data-phase-rows]")
          .append(appearanceRow(key, title, target.phases[key], false, target.mode));
      for (const [key, title] of Object.entries(effects))
        root
          .querySelector("[data-effect-rows]")
          .append(appearanceRow(key, title, target.effects[key], true, target.mode));
      field(root, "mode").addEventListener("change", () => targetMode(root));
      root.querySelector("[data-remove]").addEventListener("click", () => {
        collect();
        current.targets = current.targets.filter((item) => item.id !== target.id);
        markChanged();
        render();
      });
      root.querySelector("[data-test]").addEventListener("click", () =>
        work(async () => {
          await api(`/api/wled/${selected}/test`, "POST", {
            target_id: target.id,
            phase: root.querySelector("[data-test-phase]").value,
            value:
              target.mode === "matrix"
                ? Number(root.querySelector("[data-test-value]").value)
                : null,
            duration: 3,
          });
          notice("Test applied for 3 seconds. Automatic lighting resumes afterward.");
        }),
      );
      targetMode(root);
      $("wled-targets").append(root);
    });
    renderStatus();
    buttons();
  }

  function collectRules(root, selector, mode) {
    const result = {};
    root
      .querySelector(selector)
      .querySelectorAll("[data-rule]")
      .forEach((row) => {
        const enabled = row.querySelector('[data-value="enabled"]');
        if (enabled && !enabled.checked) return;
        const value = (name) => row.querySelector(`[data-value="${name}"]`).value;
        result[row.dataset.rule] = {
          color: value("color"),
          brightness: Number(value("brightness")),
          effect: mode === "segment" ? Number(value("effect")) : 0,
        };
        if (enabled) result[row.dataset.rule].duration = Number(value("duration"));
      });
    return result;
  }

  function collect() {
    configuration.enabled = $("wled-enabled").checked;
    const current = device();
    if (!current) return;
    current.name = $("wled-name").value.trim();
    current.url = $("wled-url").value.trim();
    current.enabled = $("wled-device-enabled").checked;
    $("wled-targets")
      .querySelectorAll("[data-target]")
      .forEach((root) => {
        const target = current.targets.find((item) => item.id === root.dataset.target);
        ["name", "mode"].forEach((key) => {
          target[key] = field(root, key).value;
        });
        ["segment", "start", "count"].forEach((key) => {
          target[key] = Number(field(root, key).value);
        });
        target.enabled = field(root, "enabled").checked;
        target.phases = collectRules(root, "[data-phase-rows]", target.mode);
        target.effects = collectRules(root, "[data-effect-rows]", target.mode);
        target.matrix = {
          width: Number(matrixField(root, "width").value),
          height: Number(matrixField(root, "height").value),
          rotation: Number(matrixField(root, "rotation").value),
          source: matrixField(root, "source").value,
          serpentine: matrixField(root, "serpentine").checked,
          appearance: {
            color: matrixField(root, "color").value,
            brightness: Number(matrixField(root, "brightness").value),
            effect: 0,
          },
        };
      });
  }

  function renderStatus() {
    if (!latestStatus) return;
    $("wled-phase").textContent = phases[latestStatus.game.phase] || latestStatus.game.phase;
    $("wled-reason").textContent = latestStatus.game.reason;
    const list = $("wled-device-status");
    list.replaceChildren();
    latestStatus.devices.forEach((item) => {
      const line = document.createElement("p");
      line.textContent = `${item.name}: ${!latestStatus.enabled || !item.enabled ? "Automation disabled" : item.connected ? "Connected" : "Not connected"}${item.error ? ` — ${item.error}` : ""}`;
      list.append(line);
    });
    const info = latestStatus.devices.find((item) => item.id === selected)?.info;
    $("wled-capabilities").textContent = info
      ? `WLED ${info.version} · ${info.segments.map((segment) => `${segment.name} (ID ${segment.id}, ${segment.length} LEDs)`).join(" · ")}${info.on === false || info.brightness === 0 ? " · Master power or brightness is off in WLED." : ""}`
      : "Save the device, then check its connection to load segments and effects.";
  }

  async function load() {
    configuration = await api("/api/wled");
    changed = false;
    render();
  }

  function refreshWled() {
    if (page !== "wled" || working) return;
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        latestStatus = await api("/api/wled/status");
        if (!configuration) await load();
        renderStatus();
        $("wled-error").textContent = latestStatus.error || "";
        $("wled-error").hidden = !latestStatus.error;
      } catch (error) {
        $("wled-error").textContent = error.message;
        $("wled-error").hidden = false;
      } finally {
        refreshing = null;
        buttons();
      }
    })();
    return refreshing;
  }

  async function work(task) {
    if (working) return;
    working = true;
    const controls = [...form.elements];
    controls.forEach((control) => {
      control.disabled = true;
    });
    try {
      await refreshing;
      await task();
    } catch (error) {
      notice(error.message, true);
    } finally {
      [...form.elements].forEach((control) => {
        control.disabled = false;
      });
      working = false;
      buttons();
      await refreshWled();
    }
  }

  form.addEventListener("input", (event) => {
    if (!event.target.matches("#wled-device, [data-test-phase], [data-test-value]")) markChanged();
  });
  form.addEventListener("change", (event) => {
    if (!event.target.matches("#wled-device, [data-test-phase], [data-test-value]")) markChanged();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    collect();
    work(async () => {
      await api("/api/wled", "PUT", configuration);
      await load();
      notice("WLED settings saved.");
    });
  });
  $("wled-device").addEventListener("change", () => {
    selected = $("wled-device").value;
    render();
  });
  $("wled-add-device").addEventListener("click", () => {
    addDevice("WLED controller", "http://wled.local");
  });
  $("wled-discover").addEventListener("click", () =>
    work(async () => {
      const message = $("wled-discovery-status");
      message.textContent = "Searching for WLED controllers… This takes a few seconds.";
      discovered = [];
      renderDiscovery();
      try {
        const result = await api("/api/wled/discover", "POST");
        discovered = result.devices;
        renderDiscovery();
        message.textContent = discovered.length
          ? `Found ${discovered.length} controller${discovered.length === 1 ? "" : "s"}. Choose Add to configure one.`
          : "No reachable controllers found. Check they are on OcheCore's network with mDNS enabled, or add an address manually.";
      } catch (error) {
        message.textContent = "Discovery failed. You can still add a device by address.";
        throw error;
      }
    }),
  );
  $("wled-remove-device").addEventListener("click", () => {
    collect();
    configuration.devices = configuration.devices.filter((item) => item.id !== selected);
    selected = "";
    markChanged();
    render();
  });
  $("wled-add-target").addEventListener("click", () => {
    collect();
    device().targets.push(newTarget());
    markChanged();
    render();
  });
  $("wled-reload").addEventListener("click", () => work(load));
  $("wled-probe").addEventListener("click", () =>
    work(async () => {
      const info = await api(`/api/wled/${selected}/probe`, "POST");
      if (latestStatus) latestStatus.devices.find((item) => item.id === selected).info = info;
      render();
      notice("WLED connection verified.");
    }),
  );
  window.addEventListener("hashchange", refreshWled);
  refreshWled();
  setInterval(refreshWled, 2000);
})();
