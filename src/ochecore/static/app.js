const $ = (id) => document.getElementById(id);
const fields = ["client_id", "board_id"];
const labels = {
  unconfigured: "Not configured", disconnected: "Disconnected",
  awaiting_authorization: "Awaiting approval", authenticated: "Authorized",
  waiting_for_login: "Waiting for login", connecting: "Connecting…",
  connected: "Connected", degraded: "Subscription rejected", reconnecting: "Reconnecting…", error: "Error", stopped: "Stopped",
};
let refreshing = false;
let dirty = false;
let busy = false;
let available = false;
let authState = "unconfigured";
let savedClientId = "";
let renderedEvents = "";
let boardOptions = [];
let boardLoading = false;
let nextBoardRefresh = 0;

async function api(path, method = "GET", body) {
  const options = { method };
  if (method !== "GET") {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body ?? {});
  }
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : "Check the connection settings.");
  }
  return data;
}

function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").className = error ? "error" : "";
  $("notice").hidden = false;
}

function updateButtons() {
  $("save").disabled = busy || !available || !dirty;
  $("login").disabled = busy || !available || dirty || !savedClientId
    || ["authenticated", "awaiting_authorization"].includes(authState);
  $("logout").disabled = busy || !available || ["unconfigured", "disconnected"].includes(authState);
  $("refresh-boards").disabled = busy || boardLoading || !available || authState !== "authenticated";
}

function applyConfig(config) {
  if (savedClientId !== config.client_id) {
    boardOptions = [];
    nextBoardRefresh = 0;
  }
  savedClientId = config.client_id;
  renderBoardOptions(dirty && !config.locked_fields.includes("board_id")
    ? $("board_id").value : config.board_id);
  fields.forEach((key) => {
    const locked = config.locked_fields.includes(key);
    if (!dirty || locked) $(key).value = config[key];
    $(key).disabled = locked;
  });
  $("managed").textContent = config.locked_fields.length
    ? "Locked settings are managed by the service configuration." : "";
}

function renderBoardOptions(selected) {
  const select = $("board_id");
  select.replaceChildren(new Option("No board selected", ""));
  boardOptions.forEach((board) => {
    const state = board.online === true ? "Online" : board.online === false ? "Offline" : "Status unknown";
    select.add(new Option(`${board.name} — ${state}`, board.id));
  });
  if (selected && !boardOptions.some((board) => board.id === selected)) {
    select.add(new Option(`Configured board (${selected})`, selected));
  }
  select.value = selected;
}

async function loadBoards() {
  if (boardLoading || authState !== "authenticated") return;
  boardLoading = true;
  nextBoardRefresh = Date.now() + 30000;
  $("boards-status").textContent = "Loading boards…";
  updateButtons();
  try {
    const result = await api("/api/boards");
    boardOptions = result.boards;
    renderBoardOptions(dirty ? $("board_id").value : result.selected_board_id ?? "");
    $("boards-status").textContent = boardOptions.length
      ? "Select your board, then save settings." : "No boards found on this account. Refresh after registering your board.";
  } catch (error) {
    $("boards-status").textContent = error.message;
  } finally {
    boardLoading = false;
    updateButtons();
  }
}

function renderEvents(events) {
  const signature = events.map((event) => event.id).join(",");
  if (signature === renderedEvents) return;
  renderedEvents = signature;
  $("event-list").replaceChildren();
  $("empty").hidden = events.length > 0;
  [...events].reverse().forEach((event) => {
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    const pre = document.createElement("pre");
    const time = new Date(event.received_at).toLocaleTimeString("en-GB");
    summary.textContent = `${time} · ${event.event}${event.snapshot ? " · snapshot" : ""}`;
    pre.textContent = JSON.stringify(event, null, 2);
    details.append(summary, pre);
    $("event-list").append(details);
  });
}

async function refresh() {
  if (refreshing || busy) return;
  refreshing = true;
  try {
    const [status, config, events] = await Promise.all([
      api("/api/status"), api("/api/config"), api("/api/events"),
    ]);
    available = true;
    applyConfig(config);
    authState = status.auth.state;
    $("auth-state").textContent = labels[authState] ?? authState;
    $("cloud-state").textContent = labels[status.cloud.state] ?? status.cloud.state;
    $("live").textContent = "Service online";
    $("count").textContent = status.events.normalized;
    $("match").textContent = status.cloud.match_id ?? "—";
    $("connection-error").textContent = [status.auth.error, status.cloud.error,
      ...(status.cloud.subscription_errors ?? []).map((item) => `${item.channel}: ${item.error}.`),
    ].filter(Boolean).join(" ");
    $("device").hidden = !status.auth.device;
    if (status.auth.device) {
      const device = status.auth.device;
      $("user-code").textContent = device.user_code;
      $("verification").removeAttribute("href");
      const url = new URL(device.verification_uri_complete || device.verification_uri);
      if (url.protocol === "https:") $("verification").href = url.href;
      const seconds = Math.max(0, Math.ceil(device.expires_at - Date.now() / 1000));
      $("expires").textContent = `Code expires in ${seconds} seconds.`;
    }
    renderEvents(events);
    if (authState === "authenticated") {
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
    $("connection-error").textContent = "Cannot reach OcheCore. Retrying automatically.";
  } finally {
    refreshing = false;
    updateButtons();
  }
}

async function action(work) {
  if (busy) return;
  busy = true;
  updateButtons();
  try {
    await work();
  } catch (error) {
    notice(error.message, true);
  } finally {
    busy = false;
    await refresh();
    updateButtons();
  }
}

fields.forEach((key) => $(key).addEventListener(key === "board_id" ? "change" : "input", () => {
  dirty = true;
  updateButtons();
}));

$("configuration").addEventListener("submit", (event) => {
  event.preventDefault();
  action(async () => {
    await api("/api/config", "PUT", Object.fromEntries(fields.map((key) => [key, $(key).value.trim()])));
    dirty = false;
    notice("Settings saved.");
  });
});

$("login").addEventListener("click", () => action(async () => {
  await api("/api/auth/login", "POST");
  notice("Approve the code on AutoDarts.");
}));

$("logout").addEventListener("click", () => action(async () => {
  await api("/api/auth/logout", "POST");
  notice("Account disconnected.");
}));

$("refresh-boards").addEventListener("click", () => action(loadBoards));

refresh();
setInterval(refresh, 2000);
