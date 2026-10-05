"""Terminal controls for the same service API used by the optional web interface."""

import argparse
import json
import logging
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
from ochecore.integrations.wled.service import EffectName, WLEDConfig


class ControlError(Exception):
    pass


def service_url(value: str) -> str:
    parts = urlsplit(value)
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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Run and control the OcheCore headless service.")
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
    commands.add_parser("status", help="Print connection status as JSON.")
    commands.add_parser("boards", help="List boards linked to the connected AutoDarts account.")
    config = commands.add_parser("config", help="Show configuration or update supplied fields.")
    config.add_argument("--client-id", help="OAuth application client ID.")
    config.add_argument("--board-id", help="Board UUID; use --board-id= to clear it.")
    login = commands.add_parser("login", help="Print a login code and wait for account approval.")
    login.add_argument(
        "--no-wait", action="store_true", help="Exit while approval runs in the service."
    )
    login.add_argument("--json", action="store_true", help="Print login details as JSON.")
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
    events.add_argument(
        "--raw", action="store_true", help="Show incoming AutoDarts frames instead of game events."
    )
    commands.add_parser("game", help="Show current game phase and display scores.")
    wled = commands.add_parser("wled", help="Configure and control WLED lights and matrices.")
    actions = wled.add_subparsers(dest="wled_command", required=True)
    actions.add_parser("status", help="Show devices, errors and current game phase.")
    actions.add_parser("discover", help="Find reachable WLED controllers on the service's network.")
    profiles = actions.add_parser(
        "profiles", help="List, create, select or remove lighting profiles."
    )
    profile_actions = profiles.add_subparsers(dest="profile_command")
    profile_actions.add_parser("list")
    create_profile = profile_actions.add_parser("create", help="Copy current rules or start blank.")
    create_profile.add_argument("name")
    create_profile.add_argument(
        "--blank", action="store_true", help="Start with lights off and no event or player rules."
    )
    profile_actions.add_parser("use").add_argument("profile")
    profile_actions.add_parser("delete").add_argument("profile")
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
    preview.add_argument("--duration", type=float, default=3)
    preview.add_argument("--event", choices=EffectName.__args__)
    preview.add_argument(
        "--player", type=int, choices=range(1, 11), help="Preview a player colour."
    )
    for name in ("on", "off"):
        power = actions.add_parser(name, help=f"Turn lights {name} without changing saved rules.")
        power.add_argument("device", nargs="?", help="Saved device ID; omit for all devices.")
    for name in ("enable", "disable"):
        command = actions.add_parser(name, help=f"{name.capitalize()} WLED or a saved device.")
        command.add_argument("device", nargs="?")
    caller = commands.add_parser("caller", help="Configure voices and control game announcements.")
    actions = caller.add_subparsers(dest="caller_command", required=True)
    for name in ("status", "enable", "disable", "stop"):
        actions.add_parser(name)
    voices = actions.add_parser("voices", help="List available and installed voices.")
    voices.add_argument("--language", help="Filter by language code, e.g. fr-FR.")
    install = actions.add_parser(
        "install", help="Select and download a voice, replacing the cache."
    )
    install.add_argument("voice")
    settings = actions.add_parser("config", help="Show or update caller settings.")
    settings.add_argument("--file", type=Path)
    settings.add_argument("--voice")
    settings.add_argument("--volume", type=float)
    settings.add_argument("--output", choices=["host", "browser", "both"])
    preview = actions.add_parser("test", help="Play a sample through the selected output.")
    preview.add_argument(
        "--call", choices=["score", "bust", "win", "checkout", "bull"], default="score"
    )
    preview.add_argument("--score", type=int, default=180)
    return root


def request(client, method: str, path: str, body: dict | None = None):
    options = {"json": body or {}} if method != "GET" else {}
    response = client.request(method, path, **options)
    try:
        data = response.json()
    except ValueError as exc:
        raise ControlError("The service returned an invalid response.") from exc
    if not response.is_success:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise ControlError(
            detail
            if isinstance(detail, str)
            else f"Request rejected (HTTP {response.status_code})."
        )
    return data


def print_json(data) -> None:
    print(json.dumps(data, indent=2), flush=True)


def login(client, args) -> int:
    auth = request(client, "POST", "/api/auth/login")
    if args.json:
        print_json(auth)
    elif auth.get("device"):
        device = auth["device"]
        print(f"Open: {device.get('verification_uri_complete') or device['verification_uri']}")
        print(f"Code: {device['user_code']}", flush=True)
    elif auth.get("state") == "authenticated":
        print("AutoDarts account is authenticated.")
    if args.no_wait or auth.get("state") == "authenticated":
        return 0
    if not args.json:
        print("Waiting for approval. Ctrl+C leaves approval running in the service.", flush=True)
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
        print_json(request(client, "GET", "/api/status"))
    elif args.command == "boards":
        print_json(request(client, "GET", "/api/boards"))
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
    elif args.command == "login":
        return login(client, args)
    elif args.command == "logout":
        print_json(request(client, "POST", "/api/auth/logout"))
    elif args.command == "events":
        if args.debug:
            print_json(
                request(
                    client,
                    "GET" if args.debug == "status" else "PUT",
                    "/api/events/debug",
                    {"enabled": args.debug == "on"},
                )
            )
        elif args.follow:
            follow_events(args.url, args.raw)
        else:
            print_json(request(client, "GET", "/api/events/raw" if args.raw else "/api/events"))
    elif args.command == "game":
        print_json(request(client, "GET", "/api/game"))
    elif args.command == "wled":
        execute_wled(args, client)
    elif args.command == "caller":
        execute_caller(args, client)
    return 0


def execute_wled(args, client) -> None:
    command = args.wled_command
    if command == "status":
        print_json(request(client, "GET", "/api/wled/status"))
    elif command == "discover":
        print_json(request(client, "POST", "/api/wled/discover"))
    elif command == "profiles":
        config = request(client, "GET", "/api/wled")
        action = args.profile_command
        if action in {None, "list"}:
            print_json(
                {
                    "active_profile": config["active_profile"],
                    "profiles": [{"id": p["id"], "name": p["name"]} for p in config["profiles"]],
                }
            )
        elif action == "create":
            print_json(
                request(
                    client,
                    "POST",
                    "/api/wled/profiles",
                    {"name": args.name, "source": "blank" if args.blank else "current"},
                )
            )
        else:
            profile = next(
                (p for p in config["profiles"] if args.profile in {p["id"], p["name"]}), None
            )
            if profile is None:
                raise ControlError("Unknown lighting profile. Use 'wled profiles' to list them.")
            print_json(
                request(client, "PUT", "/api/wled/profile", {"id": profile["id"]})
                if action == "use"
                else request(client, "DELETE", f"/api/wled/profiles/{profile['id']}")
            )
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
            except (OSError, ValueError, ValidationError) as exc:
                raise ControlError(
                    "Cannot load WLED configuration. Check the JSON file and fields."
                ) from exc
            request(client, "PUT", "/api/wled", config.model_dump(mode="json"))
        print_json(request(client, "GET", "/api/wled"))
    elif command in {"enable", "disable"}:
        update = {"enabled": command == "enable"}
        if args.device:
            config = request(client, "GET", "/api/wled")
            target = next((d for d in config["devices"] if d["id"] == args.device), None)
            if target is None:
                raise ControlError("Unknown WLED device. Save it first.")
            target["enabled"] = command == "enable"
            update = {"devices": config["devices"]}
        print_json(request(client, "PATCH", "/api/wled", update))
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
            except (ValueError, OSError) as exc:
                raise ControlError(
                    "Cannot load caller configuration. Check the JSON file."
                ) from exc
            request(client, "PUT", "/api/caller", config.model_dump())
        updates = {
            key: getattr(args, key)
            for key in ("voice", "volume", "output")
            if getattr(args, key) is not None
        }
        if updates:
            request(client, "PATCH", "/api/caller", updates)
        print_json(request(client, "GET", "/api/caller"))
    elif command in {"enable", "disable"}:
        print_json(request(client, "PATCH", "/api/caller", {"enabled": command == "enable"}))
    elif command == "voices":
        voices = request(client, "GET", "/api/caller/voices")
        print_json([v for v in voices if not args.language or v["language"] == args.language])
    elif command == "install":
        from ochecore.integrations.caller.voices import VOICES

        if args.voice not in VOICES:
            raise ControlError("Unknown voice. Run caller voices to find its ID.")
        print_json(request(client, "POST", f"/api/caller/voices/{args.voice}/install"))
    elif command == "test":
        print_json(
            request(client, "POST", "/api/caller/test", {"call": args.call, "score": args.score})
        )
    else:
        print_json(
            request(client, "GET" if command == "status" else "POST", f"/api/caller/{command}")
        )


def run(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command in {None, "serve"}:
            from ochecore.main import create_app

            settings = Settings()
            if getattr(args, "no_ui", False):
                settings.ui_enabled = False
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
