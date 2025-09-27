"""Command line interface for VPN control, Janitza health, and polling."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Callable, Dict

from dotenv import load_dotenv

from . import settings
from .janitza_client import JanitzaUMG, load_umg_config
from .poll import aligned_poll_once, poll_loop
from .vpn_connection import VPNConnection

load_dotenv()

LOGGER = logging.getLogger(__name__)


def _configure_logging() -> None:
    settings.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        settings.LOG_FILE,
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    console_handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[handler, console_handler],
        force=True,
    )


def _print_json(payload: Dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def _run_vpn_command(action: str) -> Dict[str, object]:
    _configure_logging()
    connection = VPNConnection()
    if action == "start":
        return connection.connect()
    if action == "stop":
        connection.disconnect()
        return connection.status()
    if action == "status":
        return connection.status()
    raise ValueError(f"Unsupported VPN action: {action}")


def _run_umg_health() -> Dict[str, object]:
    _configure_logging()
    cfg = load_umg_config()
    client = JanitzaUMG(
        host=cfg.get("host"),
        http_port=cfg.get("http_port"),
        modbus_port=cfg.get("modbus_port"),
        timeout_s=cfg.get("timeout_s"),
        registers=cfg.get("registers"),
    )
    payload = client.health()
    return payload


def _run_poll_once() -> Dict[str, object]:
    _configure_logging()
    return aligned_poll_once()


def _run_poll_loop(minutes: float, cycles: int | None) -> None:
    _configure_logging()
    interval_s = max(1, int(minutes * 60))
    poll_loop(interval_s=interval_s, cycles=cycles, align_to_minute=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("vpn-start")
    subparsers.add_parser("vpn-stop")
    subparsers.add_parser("vpn-status")
    subparsers.add_parser("umg-health")
    subparsers.add_parser("poll-once")

    poll_loop_parser = subparsers.add_parser("poll-loop")
    poll_loop_parser.add_argument("--minutes", type=float, default=1.0)
    poll_loop_parser.add_argument("--cycles", type=int, default=1)

    args = parser.parse_args(argv)

    try:
        if args.command == "vpn-start":
            result = _run_vpn_command("start")
            _print_json(result)
        elif args.command == "vpn-stop":
            result = _run_vpn_command("stop")
            _print_json(result)
        elif args.command == "vpn-status":
            result = _run_vpn_command("status")
            _print_json(result)
        elif args.command == "umg-health":
            result = _run_umg_health()
            _print_json(result)
        elif args.command == "poll-once":
            result = _run_poll_once()
            _print_json(result)
        elif args.command == "poll-loop":
            cycles = args.cycles if args.cycles and args.cycles > 0 else None
            _run_poll_loop(minutes=args.minutes, cycles=cycles)
        else:  # pragma: no cover
            parser.error(f"Unknown command {args.command}")
    except Exception as exc:  # pragma: no cover - runtime failures
        logging.getLogger(__name__).exception("Command failed: %s", exc)
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
