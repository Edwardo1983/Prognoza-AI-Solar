"""Command line interface for VPN control and Janitza health checks."""
from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Callable, Dict

from . import settings
from .janitza_client import JanitzaUMG
from .vpn_connection import VPNConnection


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
    client = JanitzaUMG()
    return client.health()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands: Dict[str, Callable[[], Dict[str, object]]] = {
        "vpn-start": lambda: _run_vpn_command("start"),
        "vpn-stop": lambda: _run_vpn_command("stop"),
        "vpn-status": lambda: _run_vpn_command("status"),
        "umg-health": _run_umg_health,
    }

    if not argv or argv[0] not in commands:
        sys.stderr.write(
            "Usage: python -m app [vpn-start|vpn-stop|vpn-status|umg-health]\n"
        )
        return 1

    action = argv[0]
    result = commands[action]()
    _print_json(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
