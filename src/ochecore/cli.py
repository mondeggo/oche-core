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
from ochecore.wled import WLEDConfig


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
    events.add_argument("--follow", action="store_true", help="Stream new events as JSON lines.")
    events.add_argument(
        "--raw", action="store_true", help="Show incoming AutoDarts frames instead of game events."
    )
    commands.add_parser("game", help="Show current game phase and display scores.")
    wled = commands.add_parser("wled", help="Configure and control WLED lights and matrices.")
    actions = wled.add_subparsers(dest="wled_command", required=True)
    actions.add_parser("status", help="Show devices, errors and current game phase.")
    settings = actions.add_parser("config", help="Show configuration or load a JSON file.")
    settings.add_argument("--file", type=Path)
    probe = actions.add_parser("probe", help="Check a saved controller and its segments.")
    probe.add_argument("device")
    preview = actions.add_parser("test", help="Temporarily preview a saved lighting target.")
    preview.add_argument("device")
    preview.add_argument("--target", required=True)
    preview.add_argument(
        "--phase", choices=["idle", "ready", "takeout", "waiting"], default="ready"
    )
    preview.add_argument("--value", type=int, help="Sample matrix score.")
    preview.add_argument("--duration", type=float, default=3)
    for name in ("enable", "disable"):
        command = actions.add_parser(name, help=f"{name.capitalize()} WLED or a saved device.")
        command.add_argument("device", nargs="?")
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
        if args.follow:
            follow_events(args.url, args.raw)
        else:
            print_json(request(client, "GET", "/api/events/raw" if args.raw else "/api/events"))
    elif args.command == "game":
        print_json(request(client, "GET", "/api/game"))
    elif args.command == "wled":
        execute_wled(args, client)
    return 0


def execute_wled(args, client) -> None:
    command = args.wled_command
    if command == "status":
        print_json(request(client, "GET", "/api/wled/status"))
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
        config = request(client, "GET", "/api/wled")
        target = config
        if args.device:
            target = next((d for d in config["devices"] if d["id"] == args.device), None)
            if target is None:
                raise ControlError("Unknown WLED device. Save it first.")
        target["enabled"] = command == "enable"
        print_json(request(client, "PUT", "/api/wled", config))
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
                "value": args.value,
                "duration": args.duration,
            }
        )
        print_json(request(client, "POST", f"/api/wled/{device}/{command}", body))


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
            base_url=args.url, timeout=10, follow_redirects=False, trust_env=False
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
