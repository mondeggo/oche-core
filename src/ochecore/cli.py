"""Terminal controls for the same service API used by the optional web interface."""

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import uvicorn
from pydantic import ValidationError
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from ochecore import __version__
from ochecore.config import Settings
from ochecore.integrations.caller.service import CallerConfig
from ochecore.integrations.wled.service import Device, EffectName, WLEDConfig


class ControlError(Exception):
    pass


def service_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Invalid service address.") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise argparse.ArgumentTypeError("Use an HTTP(S) service origin without credentials.")
    try:
        _ = parts.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Invalid service port.") from exc
    return value.rstrip("/")


def bounded_number(low: float, high: float):
    def parse(value: str) -> float:
        try:
            number = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("Enter a number.") from exc
        if not math.isfinite(number) or not low <= number <= high:
            raise argparse.ArgumentTypeError(f"Use a number between {low:g} and {high:g}.")
        return number

    return parse


def add_common_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print machine-readable JSON (event streams remain JSON lines).",
    )
    if "--url" not in command._option_string_actions:
        command.add_argument(
            "--url",
            type=service_url,
            default=argparse.SUPPRESS,
            help="OcheCore control API origin; may also be set with OCHECORE_URL.",
        )
    for action in command._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                add_common_options(child)


def add_ui_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--embedded",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use the compact interface intended for embedding in Oche.",
    )
    command.add_argument("--theme", choices=["dark", "light"], help="Shared interface theme.")
    command.add_argument(
        "--parent-origin",
        help="Exact Oche origin allowed to frame the UI; use an empty value to clear.",
    )


def add_profile_commands(command: argparse.ArgumentParser) -> None:
    actions = command.add_subparsers(dest="profile_command")
    actions.add_parser("list", help="List saved application profiles.")
    create = actions.add_parser("create", help="Copy current integration settings or start blank.")
    create.add_argument("name")
    source = create.add_mutually_exclusive_group()
    source.add_argument("--source", choices=["current", "blank"], default="current")
    source.add_argument(
        "--blank",
        dest="source",
        action="store_const",
        const="blank",
        help="Alias for --source blank.",
    )
    actions.add_parser("use", help="Activate an application profile.").add_argument("profile")
    actions.add_parser("delete", help="Remove a saved application profile.").add_argument("profile")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Run and control the OcheCore headless service.")
    root.set_defaults(json=False)
    root.add_argument("--version", action="version", version=f"OcheCore {__version__}")
    root.add_argument(
        "--url",
        type=service_url,
        default=os.environ.get("OCHECORE_URL", "http://127.0.0.1:9180"),
        help="Control API origin (default: OCHECORE_URL or http://127.0.0.1:9180).",
    )
    commands = root.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="Start the service (also the default command).")
    serve.add_argument("--no-ui", action="store_true", help="Disable web pages and static assets.")
    add_ui_options(serve)
    ui = commands.add_parser("ui", help="Show or change embedding and shared appearance settings.")
    add_ui_options(ui)
    profiles = commands.add_parser(
        "profiles", help="Manage application profiles for WLED and Caller."
    )
    add_profile_commands(profiles)
    commands.add_parser("status", help="Show connection status and the selected board.")
    commands.add_parser("doctor", help="Check service, connection and integration readiness.")
    commands.add_parser("boards", help="List boards linked to the connected AutoDarts account.")
    config = commands.add_parser("config", help="Show configuration or update supplied fields.")
    config.add_argument("--client-id", help="OAuth application client ID.")
    config.add_argument("--board-id", help="Board UUID; use --board-id= to clear it.")
    login = commands.add_parser("login", help="Print a login code and wait for account approval.")
    login.add_argument(
        "--no-wait", action="store_true", help="Exit while approval runs in the service."
    )
    commands.add_parser("logout", help="Remove the service's saved AutoDarts session.")
    events = commands.add_parser("events", help="Print recent events as JSON.")
    event_mode = events.add_mutually_exclusive_group()
    event_mode.add_argument(
        "--follow", action="store_true", help="Stream new events as JSON lines."
    )
    event_mode.add_argument(
        "--debug",
        choices=["on", "off", "status"],
        help="Control raw-event recording to a JSONL file on the service.",
    )
    event_actions = events.add_subparsers(dest="event_command")
    debug = event_actions.add_parser("debug", help="Control and download raw debug recordings.")
    debug_actions = debug.add_subparsers(dest="debug_command", required=True)
    for name in ("on", "off", "status"):
        debug_actions.add_parser(name)
    download = debug_actions.add_parser("download", help="Download the latest stopped recording.")
    download.add_argument("--output", type=Path, required=True, help="New local JSONL file path.")
    events.add_argument(
        "--raw", action="store_true", help="Show incoming AutoDarts frames instead of game events."
    )
    commands.add_parser("game", help="Show current game phase and display scores.")
    wled = commands.add_parser("wled", help="Configure and control WLED lights and matrices.")
    actions = wled.add_subparsers(dest="wled_command", required=True)
    actions.add_parser("status", help="Show devices, errors and current game phase.")
    actions.add_parser("discover", help="Find reachable WLED controllers on the service's network.")
    devices = actions.add_parser("devices", help="List or add WLED controllers.")
    device_actions = devices.add_subparsers(dest="device_command")
    device_actions.add_parser("list")
    add_device = device_actions.add_parser(
        "add", help="Save a controller without changing its lights."
    )
    add_device.add_argument("id", help="Unique ID, e.g. board.")
    add_device.add_argument("--name", help="Display name; defaults to the ID.")
    add_device.add_argument("--url", dest="device_url", required=True, help="WLED address.")
    targets = actions.add_parser("targets", help="List saved lighting targets and their IDs.")
    target_actions = targets.add_subparsers(dest="target_command", required=True)
    list_targets = target_actions.add_parser("list")
    list_targets.add_argument("device", help="Saved device ID.")
    profiles = actions.add_parser(
        "profiles", help="Compatibility alias for application profiles; also changes Caller."
    )
    add_profile_commands(profiles)
    settings = actions.add_parser("config", help="Show configuration or load a JSON file.")
    settings.add_argument("--file", type=Path)
    probe = actions.add_parser("probe", help="Check a saved controller or an unsaved address.")
    probe.add_argument("device", nargs="?")
    probe.add_argument("--url", dest="device_url", help="Check an address without saving it.")
    preview = actions.add_parser(
        "test", help="Temporarily preview a saved or draft lighting target."
    )
    preview.add_argument("device", nargs="?")
    preview.add_argument(
        "--file", type=Path, help="JSON device definition to preview without saving."
    )
    preview.add_argument("--target", required=True)
    preview.add_argument(
        "--phase", choices=["idle", "ready", "takeout", "waiting"], default="ready"
    )
    preview.add_argument("--value", type=int, help="Sample matrix score.")
    preview.add_argument(
        "--duration",
        type=bounded_number(0.5, 10),
        default=3,
        help="Preview duration in seconds, from 0.5 to 10 (default: 3).",
    )
    preview.add_argument("--event", choices=EffectName.__args__)
    preview.add_argument(
        "--player", type=int, choices=range(1, 11), help="Preview a player colour."
    )
    for name in ("on", "off"):
        power = actions.add_parser(name, help=f"Turn lights {name} without changing saved rules.")
        power.add_argument("device", nargs="?", help="Saved device ID; omit for all devices.")
    for name in ("enable", "disable"):
        command = actions.add_parser(
            name, help=f"{name.capitalize()} event automation; use on/off for light power."
        )
        command.add_argument("device", nargs="?")
    caller = commands.add_parser("caller", help="Configure voices and control game announcements.")
    actions = caller.add_subparsers(dest="caller_command", required=True)
    for name, help_text in {
        "status": "Show selected voice, download state and audio output.",
        "enable": "Enable automatic game announcements.",
        "disable": "Disable announcements while keeping caller settings.",
        "stop": "Stop current and queued sound; later events can play again.",
    }.items():
        actions.add_parser(name, help=help_text)
    voices = actions.add_parser("voices", help="List available and installed voices.")
    voices.add_argument("--language", help="Filter by language code, e.g. fr-FR.")
    install = actions.add_parser(
        "install", help="Select and download a voice, replacing the cache."
    )
    install.add_argument("voice")
    settings = actions.add_parser("config", help="Show or update caller settings.")
    settings.add_argument("--file", type=Path)
    settings.add_argument("--voice")
    settings.add_argument(
        "--volume", type=bounded_number(0, 1), help="Volume from 0 (mute) to 1 (full)."
    )
    settings.add_argument("--output", choices=["host", "browser", "both"])
    settings.add_argument("--darts", choices=["auto", "segment", "score", "off"])
    for name, help_text in {
        "turn-totals": "Announce completed visit totals.",
        "checkouts": "Announce checkout reminders.",
        "players": "Announce player names.",
        "include-bots": "Include bot players.",
        "local-only": "Announce only players on the local board.",
    }.items():
        settings.add_argument(
            f"--{name}", action=argparse.BooleanOptionalAction, default=None, help=help_text
        )
    preview = actions.add_parser("test", help="Play a sample through the selected output.")
    preview.add_argument(
        "--call", choices=["score", "bust", "win", "checkout", "bull"], default="score"
    )
    preview.add_argument("--score", type=int, default=180, help="Sample score from 0 to 180.")
    add_common_options(root)
    return root


def validation_message(errors: list[dict]) -> str:
    messages = []
    for error in errors:
        field = ".".join(str(part) for part in error.get("loc", []) if part != "body")
        message = error.get("msg", "Invalid value")
        messages.append(f"{field}: {message}" if field else message)
    return "; ".join(messages)


def response_data(response):
    try:
        data = response.json()
    except ValueError as exc:
        raise ControlError("The service returned an invalid response.") from exc
    if not response.is_success:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise ControlError(
            detail
            if isinstance(detail, str)
            else validation_message(detail)
            if isinstance(detail, list)
            else f"Request rejected (HTTP {response.status_code})."
        )
    return data


def request(client, method: str, path: str, body: dict | None = None, *, headers=None):
    options = {"json": body or {}} if method != "GET" else {}
    if headers:
        options["headers"] = headers
    return response_data(client.request(method, path, **options))


def print_json(data) -> None:
    print(json.dumps(data, indent=2), flush=True)


def print_table(headers: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        print("None configured.")
        return
    values = [headers, *(tuple(str(value) for value in row) for row in rows)]
    widths = [max(len(row[index]) for row in values) for index in range(len(headers))]
    for row in values:
        print(
            "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        )


def state_label(value) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"


def print_summary(data, args, kind: str) -> None:
    if args.json:
        print_json(data)
        return
    if kind == "status":
        auth, cloud = data["auth"], data["cloud"]
        print(f"Account: {auth['state']}\nCloud: {cloud['state']}")
        print(f"Board: {cloud.get('board_id') or 'not selected'}")
        print(f"Board online: {state_label(cloud.get('board_online'))}")
        for section in (auth, cloud):
            if section.get("error"):
                print(f"Attention: {section['error']}")
        if cloud.get("subscription_errors"):
            print(f"Subscription errors: {len(cloud['subscription_errors'])}")
        print("Use 'ochecore doctor' for setup checks and next steps.")
    elif kind == "game":
        print(f"Phase: {data.get('phase', 'unknown')}")
        print(f"Reason: {data.get('reason') or '-'}")
        player = data.get("player") or {}
        print(f"Player: {player.get('name') or '-'}")
        remaining = data.get("remaining")
        visit = data.get("turn_score")
        print(
            f"Remaining: {remaining if remaining is not None else '-'}  "
            f"Visit: {visit if visit is not None else '-'}"
        )
    elif kind == "boards":
        print_table(
            ("ID", "Name", "Online", "Selected"),
            [
                (
                    board["id"],
                    board["name"],
                    state_label(board.get("online")),
                    "*" if board["id"] == data.get("selected_board_id") else "",
                )
                for board in data["boards"]
            ],
        )
    elif kind == "profiles":
        print_table(
            ("ID", "Name", "Active"),
            [
                (
                    profile["id"],
                    profile["name"],
                    "*" if profile["id"] == data["active_profile"] else "",
                )
                for profile in data["profiles"]
            ],
        )
    elif kind == "voices":
        print_table(
            ("ID", "Language", "Installed"),
            [(voice["id"], voice["language"], state_label(voice["installed"])) for voice in data],
        )
    elif kind == "caller":
        print(f"Announcements: {'enabled' if data['enabled'] else 'disabled'}")
        print(
            f"Voice: {data['voice'] or 'not selected'} "
            f"({'ready' if data['installed'] else 'not ready'})"
        )
        print(f"Output: {data['output']}  Queued calls: {data['queued']}")
        print(f"Download: {data['download'].get('state', 'idle')}")
        if data.get("error"):
            print(f"Attention: {data['error']}")
    elif kind in {"wled", "devices", "discover"}:
        if kind == "wled":
            print(f"Event automation: {'enabled' if data['enabled'] else 'disabled'}")
            print(
                f"Profile: {data['active_profile']}  Phase: {data['game'].get('phase', 'unknown')}"
            )
            print_table(
                ("ID", "Name", "Reachable", "Power", "Error"),
                [
                    (
                        device["id"],
                        device["name"],
                        state_label(device.get("connected")),
                        state_label(device.get("on")),
                        device.get("error") or "-",
                    )
                    for device in data["devices"]
                ],
            )
        else:
            print_table(
                ("ID", "Name", "Address"),
                [
                    (device.get("id", "-"), device["name"], device["url"])
                    for device in data["devices"]
                ],
            )
    elif kind == "targets":
        print_table(
            ("ID", "Name", "Mode", "Segment", "Enabled"),
            [
                (
                    target["id"],
                    target["name"],
                    target["mode"],
                    target["segment"],
                    state_label(target["enabled"]),
                )
                for target in data
            ],
        )


def doctor(client, args) -> int:
    health = request(client, "GET", "/healthz")
    status = request(client, "GET", "/api/status")
    config = request(client, "GET", "/api/config")
    wled = request(client, "GET", "/api/wled/status")
    caller = request(client, "GET", "/api/caller/status")
    checks = []

    def check(name, ok, detail, next_step=""):
        checks.append(
            {"name": name, "ok": bool(ok), "detail": detail, "next_step": "" if ok else next_step}
        )

    check("Service", True, f"OcheCore {health['version']}")
    check(
        "Account",
        status["auth"]["state"] == "authenticated",
        status["auth"]["state"],
        "Run ochecore login."
        if config["client_id"]
        else "Set an OAuth client ID in configuration.",
    )
    check(
        "Board",
        config["board_id"],
        config["board_id"] or "Not selected",
        "Run ochecore boards, then ochecore config --board-id ID.",
    )
    check(
        "Cloud",
        status["cloud"]["state"] == "connected",
        status["cloud"]["state"],
        status["cloud"].get("error")
        or "Check account approval, board selection and network access.",
    )
    if config["board_id"]:
        check(
            "Board online",
            status["cloud"].get("board_online") is True,
            state_label(status["cloud"].get("board_online")),
            "Start AutoDarts on the board computer and check its network connection.",
        )
    if wled["enabled"]:
        check(
            "WLED",
            wled["devices"] and not wled.get("error"),
            wled.get("error") or f"{len(wled['devices'])} saved device(s)",
            "Run ochecore wled discover or ochecore wled devices add.",
        )
        for device in wled["devices"]:
            if device["enabled"]:
                check(
                    f"WLED {device['name']}",
                    device.get("connected"),
                    device.get("error") or "Connection not checked"
                    if not device.get("connected")
                    else "Reachable",
                    f"Run ochecore wled probe {device['id']}.",
                )
    if caller["enabled"]:
        check(
            "Caller voice",
            caller["installed"] and not caller.get("error"),
            caller.get("error") or caller["voice"] or "Not selected",
            "Run ochecore caller voices, then ochecore caller install VOICE_ID.",
        )
        if caller["output"] in {"browser", "both"}:
            check(
                "Browser sound",
                caller["browser_listeners"] > 0,
                f"{caller['browser_listeners']} connected audio browser(s)",
                "Open Caller in the UI and choose Enable sound here.",
            )
    ready = all(item["ok"] for item in checks)
    if args.json:
        print_json({"ready": ready, "checks": checks})
    else:
        for item in checks:
            print(f"{'OK' if item['ok'] else 'ACTION'}  {item['name']}: {item['detail']}")
            if item["next_step"]:
                print(f"        {item['next_step']}")
        print("Checks read saved status; lights and audio were not changed.")
    return 0 if ready else 1


def login(client, args) -> int:
    auth = request(client, "POST", "/api/auth/login")
    progress = sys.stderr if args.json else sys.stdout
    if auth.get("device"):
        device = auth["device"]
        print(
            f"Open: {device.get('verification_uri_complete') or device['verification_uri']}",
            file=progress,
        )
        print(f"Code: {device['user_code']}", file=progress, flush=True)
    if args.no_wait or auth.get("state") == "authenticated":
        if args.json:
            print_json(auth)
        elif auth.get("state") == "authenticated":
            print("AutoDarts account is authenticated.")
        return 0
    print(
        "Waiting for approval. Ctrl+C leaves approval running in the service.",
        file=progress,
        flush=True,
    )
    while auth.get("state") == "awaiting_authorization":
        time.sleep(2)
        auth = request(client, "GET", "/api/status")["auth"]
    if args.json:
        print_json(auth)
    if auth.get("state") != "authenticated":
        raise ControlError(auth.get("error") or "Login ended without account approval.")
    if not args.json:
        print("AutoDarts account is authenticated.")
    return 0


def follow_events(url: str, raw: bool = False) -> None:
    parts = urlsplit(url)
    ws_url = urlunsplit(
        (
            "wss" if parts.scheme == "https" else "ws",
            parts.netloc,
            "/events/raw" if raw else "/events",
            "",
            "",
        )
    )
    with connect(ws_url, open_timeout=10, close_timeout=3, max_size=2**20, proxy=None) as ws:
        for raw in ws:
            print(json.dumps(json.loads(raw)), flush=True)
    raise ControlError("Event stream closed. Run events --follow to reconnect.")


def execute(args, client) -> int:
    if args.command == "status":
        print_summary(request(client, "GET", "/api/status"), args, "status")
    elif args.command == "doctor":
        return doctor(client, args)
    elif args.command == "boards":
        print_summary(request(client, "GET", "/api/boards"), args, "boards")
    elif args.command == "config":
        config = request(client, "GET", "/api/config")
        updates = {
            key: value
            for key in ("client_id", "board_id")
            if (value := getattr(args, key)) is not None
        }
        if updates:
            values = {key: config[key] for key in ("client_id", "board_id")}
            request(client, "PUT", "/api/config", {**values, **updates})
            config = request(client, "GET", "/api/config")
        print_json(config)
    elif args.command == "ui":
        updates = {
            key: value
            for key in ("embedded", "theme", "parent_origin")
            if (value := getattr(args, key)) is not None
        }
        settings = request(client, "PATCH" if updates else "GET", "/api/ui", updates)
        if args.json:
            print_json(settings)
        else:
            print(f"Embedded: {state_label(settings['embedded'])}\nTheme: {settings['theme']}")
            print(f"Parent origin: {settings['parent_origin'] or 'same origin only'}")
            if settings.get("error"):
                print(f"Attention: {settings['error']}")
    elif args.command == "profiles":
        execute_profiles(args, client)
    elif args.command == "login":
        return login(client, args)
    elif args.command == "logout":
        print_json(request(client, "POST", "/api/auth/logout"))
    elif args.command == "events":
        if args.event_command and (args.debug or args.follow or args.raw):
            raise ControlError("Use either an events debug subcommand or the event stream flags.")
        if args.event_command == "debug" and args.debug_command == "download":
            download_debug(client, args)
        elif args.debug or args.event_command == "debug":
            action = args.debug if args.debug else args.debug_command
            print_json(
                request(
                    client,
                    "GET" if action == "status" else "PUT",
                    "/api/events/debug",
                    {"enabled": action == "on"},
                )
            )
        elif args.follow:
            follow_events(args.url, args.raw)
        else:
            print_json(request(client, "GET", "/api/events/raw" if args.raw else "/api/events"))
    elif args.command == "game":
        print_summary(request(client, "GET", "/api/game"), args, "game")
    elif args.command == "wled":
        execute_wled(args, client)
    elif args.command == "caller":
        execute_caller(args, client)
    return 0


def execute_profiles(args, client) -> None:
    response = client.request("GET", "/api/profiles")
    config = response_data(response)
    action = args.profile_command
    if action in {None, "list"}:
        print_summary(config, args, "profiles")
        return
    headers = {"If-Match": response.headers["etag"]} if "etag" in response.headers else None
    if action == "create":
        result = request(
            client,
            "POST",
            "/api/profiles",
            {"name": args.name, "source": args.source},
            headers=headers,
        )
    else:
        profile = next(
            (
                item
                for item in config["profiles"]
                if args.profile == item["id"] or args.profile.casefold() == item["name"].casefold()
            ),
            None,
        )
        if profile is None:
            raise ControlError("Unknown application profile. Use 'profiles list' to list them.")
        result = (
            request(client, "PUT", "/api/profile", {"id": profile["id"]}, headers=headers)
            if action == "use"
            else request(client, "DELETE", f"/api/profiles/{profile['id']}", headers=headers)
        )
    print_json(result)


def download_debug(client, args) -> None:
    if args.output.exists():
        raise ControlError("The output file already exists. Choose a new filename.")
    with client.stream("GET", "/api/events/debug/file") as response:
        if not response.is_success:
            response.read()
            response_data(response)
        try:
            output = args.output.open("xb")
        except OSError as exc:
            raise ControlError(f"Cannot create debug file: {exc}") from exc
        try:
            with output:
                for chunk in response.iter_bytes():
                    output.write(chunk)
        except BaseException as exc:
            args.output.unlink(missing_ok=True)
            if isinstance(exc, OSError):
                raise ControlError(f"Cannot save debug recording: {exc}") from exc
            raise
    if args.json:
        print_json({"saved": True, "path": str(args.output.resolve())})
    else:
        print(f"Saved debug recording to {args.output.resolve()}")


def execute_wled(args, client) -> None:
    command = args.wled_command
    if command == "status":
        print_summary(request(client, "GET", "/api/wled/status"), args, "wled")
    elif command == "discover":
        print_summary(request(client, "POST", "/api/wled/discover"), args, "discover")
    elif command == "devices":
        if args.device_command == "add":
            try:
                device = Device(id=args.id, name=args.name or args.id, url=args.device_url)
            except ValidationError as exc:
                raise ControlError(validation_message(exc.errors())) from exc
            result = request(client, "POST", "/api/wled/devices", device.model_dump(mode="json"))
            if args.json:
                print_json(result)
            else:
                print(
                    f"Saved {device.name} ({device.id}). "
                    f"Run 'ochecore wled probe {device.id}' to check it."
                )
                print("Add targets in WLED settings or import them with 'wled config --file'.")
        else:
            print_summary(request(client, "GET", "/api/wled"), args, "devices")
    elif command == "targets":
        config = request(client, "GET", "/api/wled")
        device = next((item for item in config["devices"] if item["id"] == args.device), None)
        if device is None:
            raise ControlError("Unknown WLED device. Use 'wled devices' to list them.")
        print_summary(device["targets"], args, "targets")
    elif command == "profiles":
        execute_profiles(args, client)
    elif command in {"on", "off"}:
        path = f"/api/wled/{args.device}/power" if args.device else "/api/wled/power"
        result = request(client, "POST", path, {"on": command == "on"})
        print_json(result)
        if not result["applied"]:
            raise ControlError("Some WLED devices could not be reached. See the device results.")
    elif command == "probe" and args.device_url:
        if args.device:
            raise ControlError("Choose a saved device or --url, not both.")
        print_json(request(client, "POST", "/api/wled/probe", {"url": args.device_url}))
    elif command == "test" and args.file:
        if args.device:
            raise ControlError("Choose a saved device or --file, not both.")
        try:
            device = json.loads(args.file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ControlError("Cannot load the device JSON file.") from exc
        print_json(
            request(
                client,
                "POST",
                "/api/wled/preview",
                {
                    "device": device,
                    "target_id": args.target,
                    "phase": args.phase,
                    "event": args.event,
                    "player": args.player,
                    "value": args.value,
                    "duration": args.duration,
                },
            )
        )
    elif command == "config":
        if args.file:
            try:
                config = WLEDConfig.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
            except ValidationError as exc:
                raise ControlError(validation_message(exc.errors())) from exc
            except (OSError, ValueError) as exc:
                raise ControlError(
                    "Cannot load WLED configuration. Check the JSON file and fields."
                ) from exc
            request(client, "PUT", "/api/wled", config.model_dump(mode="json"))
        print_json(request(client, "GET", "/api/wled"))
    elif command in {"enable", "disable"}:
        update = {"enabled": command == "enable"}
        path = f"/api/wled/devices/{args.device}" if args.device else "/api/wled"
        print_json(request(client, "PATCH", path, update))
    else:
        device = args.device
        if not device or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for c in device
        ):
            raise ControlError("Invalid WLED device ID.")
        body = (
            {}
            if command == "probe"
            else {
                "target_id": args.target,
                "phase": args.phase,
                "event": args.event,
                "player": args.player,
                "value": args.value,
                "duration": args.duration,
            }
        )
        print_json(request(client, "POST", f"/api/wled/{device}/{command}", body))


def execute_caller(args, client) -> None:
    command = args.caller_command
    if command == "config":
        if args.file:
            try:
                config = CallerConfig.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
            except ValidationError as exc:
                raise ControlError(validation_message(exc.errors())) from exc
            except (ValueError, OSError) as exc:
                raise ControlError(
                    "Cannot load caller configuration. Check the JSON file."
                ) from exc
            request(client, "PUT", "/api/caller", config.model_dump())
        updates = {
            key: getattr(args, key)
            for key in (
                "voice",
                "volume",
                "output",
                "darts",
                "turn_totals",
                "checkouts",
                "players",
                "include_bots",
                "local_only",
            )
            if getattr(args, key) is not None
        }
        if updates:
            request(client, "PATCH", "/api/caller", updates)
        print_json(request(client, "GET", "/api/caller"))
    elif command in {"enable", "disable"}:
        print_json(request(client, "PATCH", "/api/caller", {"enabled": command == "enable"}))
    elif command == "voices":
        voices = request(client, "GET", "/api/caller/voices")
        print_summary(
            [v for v in voices if not args.language or v["language"] == args.language],
            args,
            "voices",
        )
    elif command == "install":
        from ochecore.integrations.caller.voices import VOICES

        if args.voice not in VOICES:
            raise ControlError("Unknown voice. Run caller voices to find its ID.")
        print_json(request(client, "POST", f"/api/caller/voices/{args.voice}/install"))
    elif command == "test":
        print_json(
            request(client, "POST", "/api/caller/test", {"call": args.call, "score": args.score})
        )
    elif command == "status":
        print_summary(request(client, "GET", "/api/caller/status"), args, "caller")
    else:
        print_json(request(client, "POST", f"/api/caller/{command}"))


def run(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {None, "serve"}:
            from ochecore.main import create_app

            settings = Settings()
            if getattr(args, "no_ui", False):
                settings.ui_enabled = False
            for key in ("embedded", "theme", "parent_origin"):
                if (value := getattr(args, key, None)) is not None:
                    setattr(settings, f"ui_{key}", value)
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            logging.getLogger("httpx").setLevel(logging.WARNING)
            uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
            return 0
        with httpx.Client(
            base_url=args.url, timeout=30, follow_redirects=False, trust_env=False
        ) as client:
            return execute(args, client)
    except ControlError as exc:
        print(str(exc), file=sys.stderr)
    except ValidationError as exc:
        print(validation_message(exc.errors()), file=sys.stderr)
    except httpx.TimeoutException:
        print(
            "OcheCore did not respond in time. The operation may still be running; check status.",
            file=sys.stderr,
        )
    except (httpx.HTTPError, OSError, WebSocketException):
        print(
            "Cannot reach OcheCore. Check that the service is running and --url is correct.",
            file=sys.stderr,
        )
    except KeyboardInterrupt:
        return 130
    return 1


def main() -> None:
    raise SystemExit(run())
