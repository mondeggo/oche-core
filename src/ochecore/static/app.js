const $ = (id) => document.getElementById(id);
const fields = ["client_id", "board_id"];
const pages = {
  overview: ["Overview", "Your board and connection at a glance."],
  integrations: ["Integrations", "Enable the lights and audio you want to use."],
  autodarts: ["AutoDarts", "Manage your account and board connection."],
  wled: ["WLED", "Lighting effects for your game."],
  events: ["Events", "See what OcheCore receives from your board."],
};
const integrations = { wled: null };
const labels = {
  unconfigured: "Not configured",
  disconnected: "Disconnected",
  awaiting_authorization: "Awaiting approval",
  authenticated: "Connected",
  waiting_for_login: "Waiting for login",
  connecting: "Connecting…",
  connected: "Connected",
  degraded: "Needs attention",
  reconnecting: "Reconnecting…",
  error: "Connection error",
  stopped: "Stopped",
};
let page = "overview";
let stream = "normalized";
let refreshTask = null;
let busy = false;
let available = false;
let dirty = false;
let config = { client_id: "", board_id: "", locked_fields: [] };
let status = null;
let boardOptions = [];
let renderedBoards = "";
let boardLoading = false;
let nextBoardRefresh = 0;
let renderedEvents = "";
let eventRequest = 0;

async function api(path, method = "GET", body) {
  const options = { method };
  if (method !== "GET") {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body ?? {});
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : Array.isArray(data.detail)
          ? data.detail.map((item) => `${item.loc.slice(1).join(" / ")}: ${item.msg}`).join("; ")
          : "Check the settings.",
    );
  }
  return data;
}

function navigate(focus = false) {
  const requested = location.hash.slice(1);
  if (requested === "main") return;
  page = Object.hasOwn(pages, requested) ? requested : "overview";
  if (requested === "caller" || integrations[page] === false) {
    page = "integrations";
    history.replaceState(null, "", "#integrations");
  }
  const [title, description] = pages[page];
  document.querySelectorAll("[data-view]").forEach((view) => {
    view.hidden = view.dataset.view !== page;
  });
  document.querySelectorAll("[data-page]").forEach((link) => {
    if (link.dataset.page === page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  $("page-title").textContent = title;
  $("page-description").textContent = description;
  document.title = `${title} · OcheCore`;
  if (focus) $("page-title").focus();
  eventRequest += 1;
  refreshEvents();
}

function setIntegrationEnabled(name, enabled) {
  integrations[name] = enabled;
  document.querySelector(`[data-integration="${name}"]`).hidden = !enabled;
  if (page === name && !enabled) navigate(true);
}

function notice(message, error = false) {
  $("notice-text").textContent = message;
  $("notice").className = error ? "notice error" : "notice";
  $("notice").hidden = false;
}

function updateButtons() {
  const auth = status?.auth.state ?? "unconfigured";
  fields.forEach((key) => {
    $(key).disabled = busy || config.locked_fields.includes(key);
  });
  $("save").disabled = busy || !available || !dirty;
  $("unsaved").hidden = !dirty;
  $("login").disabled =
    busy ||
    !available ||
    dirty ||
    !config.client_id ||
    ["authenticated", "awaiting_authorization"].includes(auth);
  $("logout").disabled = busy || !available || ["unconfigured", "disconnected"].includes(auth);
  $("refresh-boards").disabled = busy || boardLoading || !available || auth !== "authenticated";
}

function applyConfig(value) {
  if (config.client_id !== value.client_id) {
    boardOptions = [];
    nextBoardRefresh = 0;
  }
  config = value;
  renderBoardOptions(
    dirty && !config.locked_fields.includes("board_id") ? $("board_id").value : config.board_id,
  );
  fields.forEach((key) => {
    if (!dirty || config.locked_fields.includes(key)) $(key).value = config[key];
  });
  $("managed").textContent = config.locked_fields.length
    ? "Locked settings are managed by config/config.yaml or environment variables."
    : "Use an OAuth client with device authorization enabled.";
}

function renderBoardOptions(selected) {
  const signature = JSON.stringify([boardOptions, selected]);
  if (signature === renderedBoards) return;
  renderedBoards = signature;
  const select = $("board_id");
  select.replaceChildren(new Option("No board selected", ""));
  boardOptions.forEach((board) => {
    const state =
      board.online === true ? "Online" : board.online === false ? "Offline" : "Status unknown";
    select.add(new Option(`${board.name} — ${state}`, board.id));
  });
  if (selected && !boardOptions.some((board) => board.id === selected)) {
    select.add(new Option(`Configured board (${selected})`, selected));
  }
  select.value = selected;
}

async function loadBoards() {
  if (boardLoading || status?.auth.state !== "authenticated") return;
  boardLoading = true;
  nextBoardRefresh = Date.now() + 30000;
  $("boards-status").textContent = "Loading boards…";
  updateButtons();
  try {
    const result = await api("/api/boards");
    boardOptions = result.boards;
    renderBoardOptions(dirty ? $("board_id").value : (result.selected_board_id ?? ""));
    $("boards-status").textContent = config.locked_fields.includes("board_id")
      ? "This board is managed by the service configuration."
      : boardOptions.length
        ? "Choose your board, then save settings."
        : "No boards found. Register a board on AutoDarts, then refresh.";
    renderStatus();
  } catch (error) {
    $("boards-status").textContent = error.message;
  } finally {
    boardLoading = false;
    updateButtons();
  }
}

function badge(id, text, tone = "") {
  $(id).textContent = text;
  $(id).dataset.tone = tone;
}

function renderStatus() {
  const { auth, cloud, events } = status;
  const authLabel = labels[auth.state] ?? auth.state;
  const cloudLabel = labels[cloud.state] ?? cloud.state;
  const connected = cloud.state === "connected";
  $("auth-state").textContent = authLabel;
  $("cloud-state").textContent = cloudLabel;
  $("live").textContent = "Service online";
  $("live").dataset.online = "true";
  $("count").textContent = events.normalized;
  badge("account-state", authLabel, auth.state === "authenticated" ? "good" : "");
  $("account-hint").textContent = !config.client_id
    ? "Set an OAuth Client ID in Advanced settings below to connect."
    : auth.state === "authenticated"
      ? "Your account is connected. Select the board you want to use."
      : auth.state === "awaiting_authorization"
        ? "Approve the connection using the code below."
        : "Sign in on AutoDarts to connect your board.";
  const board = boardOptions.find((item) => item.id === config.board_id);
  $("board-name").textContent =
    board?.name || (config.board_id ? "Your AutoDarts board" : "Choose your board");
  badge(
    "board-state",
    !config.board_id
      ? "Not selected"
      : !connected || cloud.board_online == null
        ? "Status unknown"
        : cloud.board_online
          ? "Online"
          : "Offline",
    connected && cloud.board_online ? "good" : "",
  );
  $("match").textContent = !connected
    ? "Connect AutoDarts to receive game events."
    : cloud.match_id
      ? "Match in progress"
      : "No active match";
  $("detail-board").textContent = config.board_id || "No board selected";
  $("detail-match").textContent = cloud.match_id || "No active match";
  const errors = [
    auth.error,
    cloud.error,
    ...(cloud.subscription_errors ?? []).map((item) => `${item.channel}: ${item.error}.`),
  ].filter(Boolean);
  $("connection-error").textContent = errors.join(" ") || "No connection errors reported.";
  $("connection-warning").hidden = !errors.length;
  $("warning-text").textContent =
    auth.error || cloud.error || "Some AutoDarts subscriptions need attention.";
  $("device").hidden = !auth.device;
  if (auth.device) {
    const device = auth.device;
    $("user-code").textContent = device.user_code;
    $("verification").removeAttribute("href");
    try {
      const url = new URL(device.verification_uri_complete || device.verification_uri);
      if (url.protocol === "https:") $("verification").href = url.href;
    } catch {
      /* Keep invalid approval links inactive. */
    }
    const seconds = Math.max(0, Math.ceil(device.expires_at - Date.now() / 1000));
    $("expires").textContent = `Code expires in ${seconds} seconds.`;
  }
}

function renderEvents(events) {
  const signature = `${stream}:${JSON.stringify(events)}`;
  if (signature === renderedEvents) return;
  renderedEvents = signature;
  const list = $("event-list");
  const existing = new Map([...list.children].map((item) => [item.dataset.key, item]));
  const entries = events.map((event, index) => {
    const key = stream === "normalized" ? event.id : `${index}:${JSON.stringify(event)}`;
    if (existing.has(key)) return existing.get(key);
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const pre = document.createElement("pre");
    details.dataset.key = key;
    if (stream === "normalized") {
      const time = document.createElement("span");
      time.className = "event-time";
      time.textContent = new Date(event.received_at).toLocaleTimeString("en-GB");
      summary.append(time, event.event.replaceAll("_", " "));
    } else {
      summary.textContent =
        [event?.channel, event?.topic, event?.type].filter(Boolean).join(" · ") ||
        "AutoDarts frame";
    }
    pre.textContent = JSON.stringify(event, null, 2);
    details.append(summary, pre);
    return details;
  });
  list.replaceChildren(...entries.reverse());
  $("event-count").textContent = `${events.length} ${events.length === 1 ? "entry" : "entries"}`;
  $("empty").hidden = events.length > 0;
}

async function refreshEvents() {
  if (page !== "events") return;
  const request = ++eventRequest;
  try {
    const events = await api(stream === "raw" ? "/api/events/raw" : "/api/events");
    if (request !== eventRequest) return;
    renderEvents(events);
    $("event-error").hidden = true;
  } catch {
    if (request !== eventRequest) return;
    $("event-error").textContent =
      "Cannot update events. Retrying automatically; displayed entries may be out of date.";
    $("event-error").hidden = false;
  }
}

async function refreshData() {
  try {
    const [newStatus, newConfig] = await Promise.all([api("/api/status"), api("/api/config")]);
    available = true;
    status = newStatus;
    applyConfig(newConfig);
    renderStatus();
    if (status.auth.state === "authenticated") {
      if (Date.now() >= nextBoardRefresh) await loadBoards();
    } else {
      boardOptions = [];
      nextBoardRefresh = 0;
      renderBoardOptions($("board_id").value);
      $("boards-status").textContent = "Connect your account to load boards.";
    }
  } catch {
    available = false;
    $("live").textContent = "Service unavailable";
    $("live").dataset.online = "false";
    $("auth-state").textContent = $("cloud-state").textContent = "Unavailable";
    badge("account-state", "Unavailable", "warning");
    badge("board-state", "Status unknown");
    $("match").textContent = "Waiting for the service to reconnect.";
    $("warning-text").textContent = "Cannot reach OcheCore. Retrying automatically.";
    $("connection-warning").hidden = false;
  }
  updateButtons();
  await refreshEvents();
}

function refresh() {
  if (busy) return;
  if (!refreshTask)
    refreshTask = refreshData().finally(() => {
      refreshTask = null;
    });
  return refreshTask;
}

async function action(work) {
  if (busy) return;
  busy = true;
  updateButtons();
  try {
    await refreshTask;
    await work();
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    await refresh();
    updateButtons();
  }
}

fields.forEach((key) =>
  $(key).addEventListener(key === "board_id" ? "change" : "input", () => {
    dirty = fields.some((field) => $(field).value.trim() !== config[field]);
    updateButtons();
  }),
);
$("configuration").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    await api(
      "/api/config",
      "PUT",
      Object.fromEntries(fields.map((key) => [key, $(key).value.trim()])),
    );
    dirty = false;
    notice("Settings saved.");
  });
});
$("login").addEventListener("click", () =>
  action(async () => {
    await api("/api/auth/login", "POST");
    notice("Approve the code on AutoDarts.");
  }),
);
$("logout").addEventListener("click", () =>
  action(async () => {
    await api("/api/auth/logout", "POST");
    notice("Account disconnected.");
  }),
);
$("refresh-boards").addEventListener("click", () => action(loadBoards));
$("dismiss-notice").addEventListener("click", () => {
  $("notice").hidden = true;
});

document.querySelectorAll("[data-stream]").forEach((button) =>
  button.addEventListener("click", () => {
    if (stream === button.dataset.stream) return;
    stream = button.dataset.stream;
    document.querySelectorAll("[data-stream]").forEach((item) => {
      item.setAttribute("aria-pressed", String(item.dataset.stream === stream));
    });
    $("stream-description").textContent =
      stream === "raw"
        ? "Incoming AutoDarts frames, newest first. Known credential fields are redacted."
        : "Normalized game events, newest first. Open an event to see its payload.";
    $("empty").querySelector("h2").textContent =
      stream === "raw" ? "No AutoDarts frames yet" : "No game events yet";
    $("empty").querySelector("p").textContent =
      stream === "raw"
        ? "Frames appear when the connected AutoDarts stream sends data."
        : "Start a match and throw a dart on your connected board.";
    $("event-list").replaceChildren();
    renderedEvents = "";
    renderEvents([]);
    refreshEvents();
  }),
);
window.addEventListener("hashchange", () => navigate(true));
navigate();
refresh();
setInterval(refresh, 2000);
