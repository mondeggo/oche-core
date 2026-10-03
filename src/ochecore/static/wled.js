/* WLED forms edit service configuration; device traffic stays in the headless core. */
(() => {
  let configuration = null;
  let selected = "";
  let changed = false;
  let working = false;
  let refreshing = null;
  let latestStatus = null;
  let statusError = "";
  let discovered = [];
  let saveTimer = null;
  let saveError = "";
  let saving = null;
  let editRevision = 0;
  const capabilities = new Map();
  const selectedOutputs = new Map();
  const selectedTabs = new Map();
  let outputSignature = "";
  const phases = {
    ready: "Ready to throw",
    takeout: "Remove darts",
    waiting: "Wait",
    idle: "Idle",
  };
  const phaseHints = {
    ready: "Your turn. Step up to the oche.",
    takeout: "Collect your darts from the board.",
    waiting: "Hold your throw until the board is ready.",
    idle: "Between games, keep the mood you like.",
  };
  const colourModes = {
    color: "Colour",
    white: "White",
    color_white: "Colour + white",
    on_off: "On / off",
    unknown: "Type unavailable",
  };
  const effectGroups = [
    {
      id: "victories",
      title: "Match & victories",
      effects: {
        leg_win: "Leg won",
        match_win: "Match won",
      },
    },
    {
      id: "hits",
      title: "Hits & throws",
      effects: {
        throw: "Any dart",
        single: "Single",
        double: "Double",
        triple: "Triple",
        outer_bull: "Outer bull (25)",
        bull: "Bullseye (50)",
        miss: "Miss",
        score_180: "180",
        bust: "Bust",
      },
    },
    {
      id: "transitions",
      title: "Game flow & transitions",
      effects: {
        match_started: "Match started",
        match_ended: "Match ended",
        turn_started: "Turn started",
        takeout_started: "Takeout started",
        takeout_finished: "Takeout finished",
        manual_reset: "Manual reset",
        calibration_started: "Calibration started",
        calibration_finished: "Calibration finished",
      },
    },
  ];
  const form = $("wled-form");
  const field = (root, name) => root.querySelector(`[data-field="${name}"]`);
  const matrixField = (root, name) =>
    root.querySelector(`[data-matrix-field="${name}"]`);
  const device = () =>
    configuration?.devices.find((item) => item.id === selected);
  const identity = (prefix) => `${prefix}-${Date.now().toString(36)}`;
  const appearance = (color = "#ffffff") => ({
    color,
    brightness: 128,
    effect: 0,
  });
  const newTarget = () => ({
    id: identity("target"),
    name: "Board lighting",
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
    players: {},
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
    $("wled-toggle").disabled = working || !latestStatus || !!statusError;
    $("wled-toggle-label").textContent = working
      ? "Saving…"
      : statusError
        ? "Unavailable"
        : !latestStatus
          ? "Loading…"
          : latestStatus.enabled
            ? "On"
            : "Off";
    if (working) {
      [...form.elements].forEach((control) => {
        control.disabled = true;
      });
      return;
    }
    $("wled-save-state").textContent =
      saveError ||
      (saving ? "Saving…" : changed ? "Waiting to save…" : "All changes saved");
    $("wled-save-state").classList.toggle("error", !!saveError);
    $("wled-retry").hidden = !saveError;
    $("wled-reload").hidden = !changed;
    $("wled-profile").disabled = !configuration || changed;
    $("wled-profile-create").disabled = $("wled-profile-blank").disabled =
      !configuration || changed || configuration.profiles.length >= 12;
    $("wled-profile-delete").disabled =
      !configuration || changed || configuration.profiles.length <= 1;
    $("wled-device").disabled = working || changed;
    $("wled-reload").disabled = working;
    $("wled-probe").disabled = working || !device();
    const saved = latestStatus?.devices.find(
      (item) =>
        item.id === selected &&
        origin(item.url) === origin($("wled-url").value.trim()),
    );
    $("wled-power-on").disabled = $("wled-power-off").disabled = !saved;
    form.querySelectorAll("[data-rule]").forEach(updateAppearance);
    $("wled-add-device").disabled = working || !configuration;
    $("wled-discover").disabled = !configuration;
    $("wled-add-device").disabled ||= configuration?.devices.length >= 8;
    form.querySelectorAll("[data-add-found]").forEach((button) => {
      const added = alreadyConfigured(
        discovered[Number(button.dataset.addFound)],
      );
      button.disabled = added || configuration.devices.length >= 8;
      button.textContent = added ? "Added" : "Add";
    });
    $("wled-name").disabled = $("wled-url").disabled = !device();
  }

  function markChanged() {
    changed = true;
    editRevision += 1;
    saveError = "";
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDraft, 500);
    buttons();
  }

  function saveDraft() {
    if (!changed || working || saving) return;
    if (!form.checkValidity()) {
      saveError = "Complete the highlighted settings to save automatically.";
      buttons();
      return;
    }
    collect();
    const devices = structuredClone(configuration.devices);
    const revision = editRevision;
    saving = (async () => {
      try {
        await api("/api/wled", "PATCH", { devices });
        const saved = await api("/api/wled");
        configuration.profiles = saved.profiles;
        configuration.active_profile = saved.active_profile;
        changed = revision !== editRevision;
        saveError = "";
        renderProfiles();
        for (const option of $("wled-device").options) {
          const item = configuration.devices.find(
            (device) => device.id === option.value,
          );
          if (item) option.textContent = item.name;
        }
      } catch (error) {
        if (revision === editRevision)
          saveError = `Not saved: ${error.message}`;
      } finally {
        saving = null;
        buttons();
        if (changed && !saveError && !working)
          saveTimer = setTimeout(saveDraft, 500);
      }
    })();
    buttons();
    return saving;
  }

  function renderProfiles() {
    const select = $("wled-profile");
    select.replaceChildren(
      ...configuration.profiles.map((item) => new Option(item.name, item.id)),
    );
    select.value = configuration.active_profile;
  }

  function origin(url) {
    try {
      return new URL(url).origin;
    } catch {
      return url;
    }
  }

  function deviceInfo() {
    const address = origin($("wled-url").value.trim());
    return (
      capabilities.get(address) ||
      latestStatus?.devices.find((item) => origin(item.url) === address)?.info
    );
  }

  function alreadyConfigured(found) {
    const address = new URL(found.url);
    const origins = [address.origin];
    if (found.hostname) {
      address.hostname = found.hostname;
      origins.push(address.origin);
    }
    return configuration.devices.some((item) =>
      origins.includes(origin(item.url)),
    );
  }

  function addDevice(name, url) {
    if (!configuration || configuration.devices.length >= 8) return;
    collect();
    const added = {
      id: identity("device"),
      name,
      url,
      enabled: true,
      targets: [],
    };
    configuration.devices.push(added);
    selected = added.id;
    markChanged();
    render();
    $("wled-connection").open = true;
    $("wled-url").focus();
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
        notice(
          "Device added. Choose its lighting targets; changes save automatically.",
        );
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
    if (min !== undefined) {
      element.min = min;
      element.max = max;
      element.required = true;
    }
    if (type === "checkbox") element.checked = value;
    else element.value = value;
    return element;
  }

  function appearanceRow(key, title, value, isEffect, mode, isPlayer = false) {
    const row = document.createElement("div");
    row.className = "wled-appearance";
    row.dataset.rule = key;
    row.classList.toggle("wled-phase-card", !isEffect);
    const heading = document.createElement("div");
    heading.className = "wled-rule-heading";
    const name = document.createElement("strong");
    name.textContent = title;
    if (isEffect) {
      const toggle = label(title, input("checkbox", Boolean(value), "enabled"));
      toggle.className = "check-label";
      heading.append(toggle);
      const state = document.createElement("span");
      state.dataset.ruleState = "";
      state.className = "hint";
      heading.append(state);
    } else {
      heading.append(name);
      const description = document.createElement("p");
      description.textContent = phaseHints[key];
      heading.append(description);
    }
    row.append(heading);
    const controls = document.createElement("div");
    controls.className = "wled-rule-controls";
    row.append(controls);
    const settings = value || { ...appearance(), duration: 2 };
    row.style.setProperty("--phase-colour", settings.color);
    if (isPlayer) {
      row.dataset.playerSlot = key;
      const filter = input("text", settings.name_filter || "", "name_filter");
      filter.maxLength = 80;
      filter.placeholder = `Player ${key} by position`;
      controls.append(label("Name contains (optional)", filter));
    }
    const select = document.createElement("select");
    select.dataset.value = "effect";
    const info = deviceInfo();
    (info?.effects || ["Solid"]).forEach((name, index) => {
      if (!["-", "RSVD"].includes(name)) select.add(new Option(name, index));
    });
    if (
      ![...select.options].some(
        (item) => Number(item.value) === settings.effect,
      )
    ) {
      select.add(new Option(`Effect ${settings.effect}`, settings.effect));
    }
    select.value = settings.effect;
    const effectLabel = label("Effect", select);
    effectLabel.dataset.nativeEffect = "";
    effectLabel.hidden = mode !== "segment";
    controls.append(effectLabel);
    const colour = label("Colour", input("color", settings.color, "color"));
    colour.dataset.colourControl = "";
    colour.querySelector("input").addEventListener("input", (event) => {
      row.style.setProperty("--phase-colour", event.target.value);
    });
    controls.append(colour);
    const brightness = label(
      "Brightness",
      input("range", settings.brightness, "brightness", 0, 255),
    );
    brightness.className = "wled-brightness";
    const level = document.createElement("output");
    brightness.append(level);
    controls.append(brightness);
    const updateLevel = () => {
      const value = Number(brightness.querySelector("input").value);
      level.textContent = value ? `${Math.round((value / 255) * 100)}%` : "Off";
      brightness
        .querySelector("input")
        .setAttribute("aria-valuetext", level.textContent);
    };
    brightness.querySelector("input").addEventListener("input", updateLevel);
    updateLevel();
    if (isEffect && !isPlayer) {
      const duration = input("number", settings.duration, "duration", 0.1, 30);
      duration.step = "0.1";
      controls.append(label("Seconds", duration));
    }
    const preview = document.createElement("button");
    preview.type = "button";
    preview.textContent = "▶ Preview";
    preview.dataset.preview = key;
    preview.setAttribute("aria-label", `Preview ${title.toLowerCase()}`);
    preview.addEventListener("click", () =>
      previewTarget(row.closest("[data-target]"), key, isEffect, isPlayer),
    );
    controls.append(preview);
    select.addEventListener("change", () => updateAppearance(row));
    row
      .querySelector('[data-value="enabled"]')
      ?.addEventListener("change", () => updateAppearance(row));
    return row;
  }

  function updateAppearance(row) {
    const enabled = row.querySelector('[data-value="enabled"]');
    const active = !enabled || enabled.checked;
    const root = row.closest("[data-target]");
    const native = field(root, "mode").value === "segment";
    const effect = Number(row.querySelector('[data-value="effect"]').value);
    const slots = deviceInfo()?.effect_colors?.[effect];
    const colorMode = deviceInfo()?.segments?.find(
      (segment) => segment.id === Number(field(root, "segment").value),
    )?.color_mode;
    row.dataset.colorMode = colorMode || "unknown";
    row.querySelector("[data-colour-control]").hidden =
      ["white", "on_off"].includes(colorMode) ||
      Boolean(native && slots && !slots[0]);
    row.classList.toggle("is-enabled", active);
    row.querySelector(".wled-rule-controls").hidden = !active;
    if (enabled)
      row.querySelector("[data-rule-state]").textContent = active
        ? "On"
        : "Off";
    row.querySelectorAll("input, select, button").forEach((control) => {
      control.disabled = working || (!active && control !== enabled);
    });
  }

  function previewTarget(
    root,
    rule = "ready",
    isEffect = false,
    isPlayer = false,
  ) {
    if (!form.reportValidity()) return;
    collect();
    const current = structuredClone(device());
    const target = current.targets.find(
      (item) => item.id === root.dataset.target,
    );
    const draft = {
      device: current,
      target_id: target.id,
      phase: isEffect ? "ready" : rule,
      event: isEffect && !isPlayer ? rule : null,
      player: isPlayer ? Number(rule) : null,
      value:
        target.mode === "matrix"
          ? Number(root.querySelector("[data-test-value]").value)
          : null,
      duration: 3,
    };
    work(async () => {
      notice("Previewing for 3 seconds…");
      await api("/api/wled/preview", "POST", draft);
      notice("Preview complete. Previous lighting restored.");
    });
  }

  function targetMode(root) {
    const mode = field(root, "mode").value;
    root.querySelector("[data-pixels]").hidden = mode === "segment";
    root.querySelector("[data-count]").hidden = mode !== "pixels";
    for (const tab of root.querySelectorAll("[data-editor-tab]")) {
      const key = tab.dataset.editorTab;
      tab.hidden =
        key === "display"
          ? mode !== "matrix"
          : key !== "events" && mode === "matrix";
    }
    const selectedTab = selectedTabs.get(root.dataset.target) || "phases";
    selectTab(
      root,
      root.querySelector(`[data-editor-tab="${selectedTab}"]`).hidden
        ? mode === "matrix"
          ? "display"
          : "phases"
        : selectedTab,
    );
    root.querySelector("[data-test-score]").hidden = mode !== "matrix";
    root.querySelectorAll("[data-native-effect]").forEach((node) => {
      node.hidden = mode !== "segment";
    });
    root.querySelectorAll("[data-rule]").forEach(updateAppearance);
  }

  function selectTab(root, key) {
    selectedTabs.set(root.dataset.target, key);
    for (const tab of root.querySelectorAll("[data-editor-tab]")) {
      const active = tab.dataset.editorTab === key;
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    for (const panel of root.querySelectorAll("[data-editor-panel]"))
      panel.hidden = panel.dataset.editorPanel !== key;
  }

  const outputLabel = (output) =>
    output.matrix
      ? output.name
      : output.pins.length
        ? `GPIO ${output.pins.join(" + ")}`
        : `Output ${output.id + 1}`;

  function deviceOutputs(info) {
    return [
      ...(info?.outputs || []),
      ...(info?.segments || [])
        .filter((segment) => segment.matrix)
        .map((segment) => ({
          id: `matrix-${segment.id}`,
          name: segment.name,
          segment: segment.id,
          matrix: segment.matrix,
          color_mode: segment.color_mode,
          pins: [],
          length: segment.length,
          type_name: `${segment.matrix.width} × ${segment.matrix.height} matrix`,
        })),
    ];
  }

  function outputsForTarget(target, info) {
    const segment = info?.segments.find((item) => item.id === target.segment);
    if (!segment) return [];
    if (segment.matrix)
      return deviceOutputs(info).filter(
        (output) => output.matrix && output.segment === segment.id,
      );
    const start =
      segment.start + (target.mode === "segment" ? 0 : target.start);
    const stop =
      target.mode === "segment"
        ? segment.stop
        : start +
          (target.mode === "matrix"
            ? target.matrix.width * target.matrix.height
            : target.count);
    return (info.outputs || []).filter(
      (output) => Math.max(start, output.start) < Math.min(stop, output.stop),
    );
  }

  function outputSegments(output, info) {
    return (info?.segments || []).filter(
      (segment) =>
        !output ||
        (output.matrix
          ? segment.id === output.segment
          : !segment.matrix &&
            segment.start >= output.start &&
            segment.stop <= output.stop),
    );
  }

  function segmentOptions(root, target, info) {
    const select = field(root, "segment");
    // Capability refreshes must preserve an edited or temporarily unavailable segment.
    const value = Number(select.value || target.segment);
    const options = (info?.segments || []).map((segment) => {
      const outputs = outputsForTarget(
        { ...target, segment: segment.id, mode: "segment" },
        info,
      );
      return new Option(
        `${segment.name} · ID ${segment.id}${segment.matrix ? ` · ${segment.matrix.width} × ${segment.matrix.height} matrix` : outputs.length ? ` · ${outputs.map(outputLabel).join(" / ")}` : ""}`,
        segment.id,
      );
    });
    if (!options.some((item) => Number(item.value) === value))
      options.push(
        new Option(`Segment ${value}${info ? " (unavailable)" : ""}`, value),
      );
    select.replaceChildren(...options);
    select.value = value;
  }

  function renderOutputs() {
    const current = device();
    if (!current) return;
    const info = deviceInfo();
    const outputs = deviceOutputs(info);
    let choice = selectedOutputs.get(current.id);
    if (
      choice !== "all" &&
      !outputs.some((output) => String(output.id) === choice)
    ) {
      const first =
        current.targets[0] && outputsForTarget(current.targets[0], info);
      choice =
        first?.length === 1
          ? String(first[0].id)
          : outputs.length
            ? String(outputs[0].id)
            : "all";
      selectedOutputs.set(current.id, choice);
    }
    const selectedOutput = outputs.find(
      (output) => String(output.id) === choice,
    );
    $("wled-outputs-section").hidden = !outputs.length;
    const signature = JSON.stringify([current.id, info?.segments, outputs]);
    if (signature !== outputSignature) {
      outputSignature = signature;
      const list = $("wled-outputs");
      list.replaceChildren();
      for (const output of [...outputs, null]) {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.output = output ? String(output.id) : "all";
        const name = document.createElement("strong");
        name.textContent = output ? outputLabel(output) : "All targets";
        const icon = document.createElement("span");
        icon.className = "wled-output-icon";
        icon.dataset.mode = output?.color_mode || "unknown";
        icon.setAttribute("aria-hidden", "true");
        const kind = document.createElement("span");
        kind.className = "wled-output-kind";
        kind.textContent = output
          ? colourModes[output.color_mode] || "Type unavailable"
          : "Overview";
        const detail = document.createElement("span");
        detail.className = "wled-output-detail";
        detail.textContent = output
          ? `${output.type_name} · ${output.length} ${output.length === 1 ? "LED" : "LEDs"}`
          : "Every lighting target";
        button.append(icon, name, kind, detail);
        button.addEventListener("click", () => {
          selectedOutputs.set(current.id, button.dataset.output);
          renderOutputs();
        });
        list.append(button);
      }
      for (const root of $("wled-targets").querySelectorAll("[data-target]")) {
        const target = current.targets.find(
          (item) => item.id === root.dataset.target,
        );
        segmentOptions(root, target, info);
      }
    }
    $("wled-outputs")
      .querySelectorAll("[data-output]")
      .forEach((button) => {
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.output === choice),
        );
        button.disabled = working;
      });
    let visible = 0;
    for (const root of $("wled-targets").querySelectorAll("[data-target]")) {
      const target = current.targets.find(
        (item) => item.id === root.dataset.target,
      );
      const matches = outputsForTarget(target, info);
      root.hidden =
        !!selectedOutput &&
        !matches.some((output) => String(output.id) === choice);
      if (!root.hidden) visible++;
      root.querySelector("[data-output-note]").textContent =
        matches.length > 1
          ? "This target spans multiple outputs. Create separate segments in WLED for independent effects."
          : matches.length
            ? `${outputLabel(matches[0])} · ${colourModes[matches[0].color_mode] || matches[0].type_name}`
            : "Output information unavailable. Check the connection.";
      root.querySelectorAll("[data-rule]").forEach(updateAppearance);
      const segment = info?.segments?.find(
        (item) => item.id === Number(field(root, "segment").value),
      );
      matrixField(root, "serpentine").closest("label").hidden =
        !!segment?.matrix;
      const geometryHint = root.querySelector("[data-matrix-layout]");
      geometryHint.hidden = !segment?.matrix;
      geometryHint.textContent =
        segment?.pixel_control_error ||
        (segment?.matrix
          ? `WLED layout: ${segment.matrix.width} × ${segment.matrix.height}. Use these dimensions with first LED 0. Wiring is managed in WLED.`
          : "");
      matrixField(root, "color").closest("label").hidden = [
        "white",
        "on_off",
      ].includes(segment?.color_mode);
    }
    const segments = outputSegments(selectedOutput, info);
    $("wled-add-target").disabled =
      working ||
      current.targets.length >= 16 ||
      (!!selectedOutput && !segments.length);
    $("wled-add-target").textContent = selectedOutput
      ? `Add target on ${outputLabel(selectedOutput)}`
      : "Add target";
    $("wled-output-empty").hidden =
      visible > 0 && (!selectedOutput || segments.length > 0);
    $("wled-target-heading").textContent = selectedOutput
      ? `Lighting on ${outputLabel(selectedOutput)}`
      : "Lighting rules";
    $("wled-output-empty-text").textContent =
      selectedOutput && !segments.length
        ? info?.matrix
          ? "For a 2D layout, choose a matrix card above. Physical GPIO outputs do not define its logical rows and columns."
          : "This output needs its own segment in WLED. Create one there, then check the connection again."
        : `Add a target${selectedOutput ? ` on ${outputLabel(selectedOutput)}` : ""} to set up phase lighting and event effects.`;
  }

  function render() {
    renderProfiles();
    const select = $("wled-device");
    select.replaceChildren(
      ...configuration.devices.map((item) => new Option(item.name, item.id)),
    );
    if (!device()) selected = configuration.devices[0]?.id || "";
    if (!selected) select.add(new Option("No devices configured", ""));
    select.value = selected;
    const current = device();
    $("wled-device-editor").hidden = !current;
    $("wled-no-device").hidden = !!current;
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
      const root = $(
        "wled-target-template",
      ).content.firstElementChild.cloneNode(true);
      root.dataset.target = target.id;
      root.querySelector("[data-title]").textContent = target.name;
      ["name", "mode", "start", "count"].forEach((key) => {
        field(root, key).value = target[key];
      });
      segmentOptions(root, target, deviceInfo());
      field(root, "segment").addEventListener("change", () => {
        collect();
        const outputs = outputsForTarget(target, deviceInfo());
        selectedOutputs.set(
          current.id,
          outputs.length === 1 ? String(outputs[0].id) : "all",
        );
        renderOutputs();
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
          .append(
            appearanceRow(key, title, target.phases[key], false, target.mode),
          );
      for (const group of effectGroups) {
        const section = document.createElement("details");
        section.className = "wled-event-group";
        section.dataset.eventGroup = group.id;
        const heading = document.createElement("summary");
        heading.textContent = group.title;
        section.open = group.id === "victories";
        section.append(heading);
        for (const [key, title] of Object.entries(group.effects))
          section.append(
            appearanceRow(key, title, target.effects[key], true, target.mode),
          );
        root.querySelector("[data-effect-rows]").append(section);
      }
      for (let slot = 1; slot <= 10; slot++)
        root
          .querySelector("[data-player-rows]")
          .append(
            appearanceRow(
              String(slot),
              `Player ${slot}`,
              target.players?.[slot],
              true,
              target.mode,
              true,
            ),
          );
      root.querySelectorAll("[data-editor-tab]").forEach((button) => {
        const key = button.dataset.editorTab;
        button.id = `${target.id}-tab-${key}`;
        const panel = root.querySelector(`[data-editor-panel="${key}"]`);
        panel.id = `${target.id}-panel-${key}`;
        button.setAttribute("aria-controls", panel.id);
        panel.setAttribute("aria-labelledby", button.id);
        button.addEventListener("click", () => selectTab(root, key));
        button.addEventListener("keydown", (event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
            return;
          event.preventDefault();
          const tabs = [...root.querySelectorAll("[data-editor-tab]")].filter(
            (tab) => !tab.hidden,
          );
          const index =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? tabs.length - 1
                : (tabs.indexOf(button) +
                    (event.key === "ArrowRight" ? 1 : -1) +
                    tabs.length) %
                  tabs.length;
          selectTab(root, tabs[index].dataset.editorTab);
          tabs[index].focus();
        });
      });
      field(root, "name").addEventListener("input", () => {
        root.querySelector("[data-title]").textContent =
          field(root, "name").value || "Lighting target";
      });
      field(root, "mode").addEventListener("change", () => targetMode(root));
      root.querySelector("[data-remove]").addEventListener("click", () => {
        collect();
        current.targets = current.targets.filter(
          (item) => item.id !== target.id,
        );
        markChanged();
        render();
      });
      root
        .querySelector("[data-test]")
        .addEventListener("click", () => previewTarget(root));
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
        const value = (name) =>
          row.querySelector(`[data-value="${name}"]`).value;
        const key = row.dataset.playerSlot || row.dataset.rule;
        result[key] = {
          color: value("color"),
          brightness: Number(value("brightness")),
          effect: mode === "segment" ? Number(value("effect")) : 0,
        };
        if (row.dataset.playerSlot)
          result[key].name_filter = value("name_filter").trim();
        else if (enabled) result[key].duration = Number(value("duration"));
      });
    return result;
  }

  function collect() {
    const current = device();
    if (!current) return;
    current.name = $("wled-name").value.trim();
    current.url = $("wled-url").value.trim();
    current.enabled = $("wled-device-enabled").checked;
    $("wled-targets")
      .querySelectorAll("[data-target]")
      .forEach((root) => {
        const target = current.targets.find(
          (item) => item.id === root.dataset.target,
        );
        ["name", "mode"].forEach((key) => {
          target[key] = field(root, key).value;
        });
        ["segment", "start", "count"].forEach((key) => {
          target[key] = Number(field(root, key).value);
        });
        target.enabled = field(root, "enabled").checked;
        target.phases = collectRules(root, "[data-phase-rows]", target.mode);
        target.effects = collectRules(root, "[data-effect-rows]", target.mode);
        target.players = collectRules(root, "[data-player-rows]", target.mode);
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
    $("wled-phase").textContent =
      phases[latestStatus.game.phase] || latestStatus.game.phase;
    $("wled-phase").dataset.phase = latestStatus.game.phase;
    $("wled-reason").textContent = latestStatus.game.reason;
    renderOutputs();
    $("wled-targets")
      .querySelectorAll(".wled-phase-card")
      .forEach((card) => {
        const active = card.dataset.rule === latestStatus.game.phase;
        if (active) card.setAttribute("aria-current", "step");
        else card.removeAttribute("aria-current");
      });
    const list = $("wled-device-status");
    list.replaceChildren();
    const item = latestStatus.devices.find(
      (item) =>
        item.id === selected &&
        origin(item.url) === origin($("wled-url").value.trim()),
    );
    if (item) {
      const state = document.createElement("span");
      state.className = "wled-connection-state";
      state.dataset.connected = String(item.connected);
      state.textContent = item.connected ? "Connected" : "Not connected";
      const detail = document.createElement("span");
      const power =
        item.on === false
          ? "Lights off"
          : item.on === true
            ? "Lights on"
            : "Power unknown";
      detail.textContent =
        item.error ||
        `${power} · ${latestStatus.enabled && item.enabled ? "Automatic lighting" : "Automation paused"}`;
      list.append(state, detail);
    } else if (device())
      list.textContent = "Check this address to verify the connection.";
    for (const on of [true, false])
      $(`wled-power-${on ? "on" : "off"}`).setAttribute(
        "aria-pressed",
        String(!!item && item.on === on),
      );
    const info = deviceInfo();
    $("wled-capabilities").textContent = info
      ? `WLED ${info.version} · ${info.segments.length} segments · ${info.effects.length} effects`
      : "Check the connection to load outputs, segments and effects.";
  }

  async function load() {
    configuration = await api("/api/wled");
    changed = false;
    saveError = "";
    render();
  }

  function refreshWled() {
    if (working) return;
    if (refreshing) return refreshing;
    refreshing = (async () => {
      try {
        latestStatus = await api("/api/wled/status");
        statusError = "";
        syncIntegration();
        if (page === "wled" && !configuration) await load();
        if (
          configuration &&
          !changed &&
          latestStatus.active_profile !== configuration.active_profile
        )
          await load();
        renderStatus();
      } catch (error) {
        statusError = error.message;
      } finally {
        for (const id of ["wled-error", "wled-integration-error"]) {
          $(id).textContent = statusError || latestStatus?.error || "";
          $(id).hidden = !$(id).textContent;
        }
        refreshing = null;
        buttons();
      }
    })();
    return refreshing;
  }

  function syncIntegration() {
    const enabled = latestStatus.enabled;
    if (configuration) configuration.enabled = enabled;
    $("wled-toggle").setAttribute("aria-checked", String(enabled));
    $("wled-settings").hidden = !enabled;
    setIntegrationEnabled("wled", enabled);
  }

  async function work(task) {
    if (working) return;
    working = true;
    clearTimeout(saveTimer);
    buttons();
    for (const id of [
      "wled-profile",
      "wled-profile-name",
      "wled-profile-create",
      "wled-profile-blank",
      "wled-profile-delete",
    ])
      $(id).disabled = true;
    try {
      await refreshing;
      await saving;
      await task();
    } catch (error) {
      notice(error.message, true);
    } finally {
      [...form.elements].forEach((control) => {
        control.disabled = false;
      });
      working = false;
      $("wled-profile-name").disabled = false;
      buttons();
      if (changed && !saveError) saveTimer = setTimeout(saveDraft, 500);
      await refreshWled();
    }
  }

  form.addEventListener("input", (event) => {
    if (!event.target.matches("#wled-device, [data-test-value]")) markChanged();
    if (event.target.id === "wled-url") renderStatus();
  });
  form.addEventListener("change", (event) => {
    if (!event.target.matches("#wled-device, [data-test-value]")) markChanged();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    saveDraft();
  });
  $("wled-retry").addEventListener("click", saveDraft);
  $("wled-profile").addEventListener("change", () => {
    const id = $("wled-profile").value;
    work(async () => {
      await api("/api/wled/profile", "PUT", { id });
      await load();
      notice("Lighting profile applied.");
    });
  });
  function createProfile(source) {
    const name = $("wled-profile-name").value.trim();
    if (!name) return $("wled-profile-name").focus();
    work(async () => {
      await api("/api/wled/profiles", "POST", { name, source });
      $("wled-profile-name").value = "";
      await load();
      notice(
        source === "blank"
          ? "Blank profile created. Choose your lighting below."
          : "Profile copied from the current colours and effects.",
      );
    });
  }
  $("wled-profile-create").addEventListener("click", () =>
    createProfile("current"),
  );
  $("wled-profile-blank").addEventListener("click", () =>
    createProfile("blank"),
  );
  $("wled-profile-delete").addEventListener("click", () =>
    work(async () => {
      await api(`/api/wled/profiles/${configuration.active_profile}`, "DELETE");
      await load();
      notice("Profile removed.");
    }),
  );
  $("wled-toggle").addEventListener("click", () => {
    const enabled = !latestStatus.enabled;
    work(async () => {
      await api("/api/wled", "PATCH", { enabled });
      latestStatus.enabled = enabled;
      syncIntegration();
      notice(
        enabled
          ? "WLED enabled. Open its settings to configure lights."
          : "WLED disabled. Settings kept.",
      );
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
      message.hidden = false;
      message.textContent =
        "Searching for WLED controllers… This takes a few seconds.";
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
        message.textContent =
          "Discovery failed. You can still add a device by address.";
        throw error;
      }
    }),
  );
  $("wled-remove-device").addEventListener("click", () => {
    collect();
    configuration.devices = configuration.devices.filter(
      (item) => item.id !== selected,
    );
    selected = "";
    markChanged();
    render();
  });
  $("wled-add-target").addEventListener("click", () => {
    collect();
    const current = device();
    const info = deviceInfo();
    const output = deviceOutputs(info).find(
      (item) => String(item.id) === selectedOutputs.get(current.id),
    );
    const segments = outputSegments(output, info);
    if (output && !segments.length) return;
    const unused = segments.find(
      (segment) =>
        !current.targets.some(
          (target) => target.enabled && target.segment === segment.id,
        ),
    );
    const segment = unused || segments[0];
    const target = newTarget();
    if (segment) {
      target.segment = segment.id;
      target.name =
        `${output ? `${outputLabel(output)} · ` : ""}${segment.name}`.slice(
          0,
          80,
        );
      target.enabled = !!unused;
      if (
        segment.matrix &&
        segment.matrix.width >= 11 &&
        segment.matrix.height >= 5 &&
        segment.matrix.width <= 64 &&
        segment.matrix.height <= 64 &&
        segment.length <= 512 &&
        !segment.pixel_control_error
      ) {
        target.mode = "matrix";
        target.matrix.width = segment.matrix.width;
        target.matrix.height = segment.matrix.height;
        target.matrix.serpentine = false;
      }
      if (["white", "on_off"].includes(segment.color_mode)) {
        for (const style of Object.values(target.phases))
          style.color = "#ffffff";
        target.phases.idle.brightness = 16;
        target.effects = {};
      }
    } else if (
      current.targets.some(
        (item) => item.enabled && item.segment === target.segment,
      )
    )
      target.enabled = false;
    current.targets.push(target);
    markChanged();
    render();
  });
  $("wled-reload").addEventListener("click", () => work(load));
  $("wled-probe").addEventListener("click", () => {
    if (!$("wled-url").reportValidity()) return;
    collect();
    work(async () => {
      const url = $("wled-url").value.trim();
      const info = await api("/api/wled/probe", "POST", { url });
      capabilities.set(origin(url), info);
      render();
      notice("WLED connection verified.");
    });
  });
  for (const on of [true, false]) {
    $(`wled-power-${on ? "on" : "off"}`).addEventListener("click", () =>
      work(async () => {
        const result = await api(`/api/wled/${selected}/power`, "POST", { on });
        if (!result.applied) throw new Error(result.devices[0].error);
        notice(
          on
            ? "Lights on. Saved automation resumes if enabled."
            : "Lights off. Saved settings kept.",
        );
      }),
    );
  }
  window.addEventListener("hashchange", refreshWled);
  refreshWled();
  setInterval(refreshWled, 2000);
})();
