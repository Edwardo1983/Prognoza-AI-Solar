"""Command-line interface for managing the OpenVPN GUI connection."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from app import settings
from app.vpn_connection import VPNConnection


def _configure_logging() -> None:
    """Configure application logging with rotation and console echo."""
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the OpenVPN GUI connection for UMG 509 PRO.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--connect", action="store_true", help="Create or refresh the VPN tunnel.")
    group.add_argument("--disconnect", action="store_true", help="Tear down the active VPN tunnel.")
    group.add_argument("--status", action="store_true", help="Report VPN tunnel status as JSON.")
    return parser


def _emit(result: Dict[str, Any]) -> None:
    print(json.dumps(result, indent=2))


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for the VPN CLI."""
    _configure_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    connection = VPNConnection()

    try:
        if args.connect:
            result = connection.connect()
            _emit(result)
        elif args.disconnect:
            connection.disconnect()
            result = connection.status()
            _emit(result)
        elif args.status:
            result = connection.status()
            _emit(result)
    except FileNotFoundError as exc:
        logging.getLogger(__name__).error("%s", exc)
        _emit(
            {
                "is_connected": False,
                "error": str(exc),
                "profile_name": settings.PROFILE_NAME,
                "log_path": str(settings.LOG_FILE),
            }
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("Unhandled error: %s", exc)
        _emit({"error": str(exc), "profile_name": settings.PROFILE_NAME})
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
