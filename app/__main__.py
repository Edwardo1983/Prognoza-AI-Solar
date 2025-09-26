"""Command line interface for the Prognoza AI Solar VPN helper."""
from __future__ import annotations

import json
import sys
from typing import Callable, Dict

from . import settings
from .vpn import extract_ovpn_assets, start_vpn, stop_vpn, vpn_status


def _print_json(result: Dict[str, object]) -> None:
    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in {"vpn-start", "vpn-stop", "vpn-status", "vpn-extract"}:
        sys.stderr.write("Usage: python -m app [vpn-start|vpn-stop|vpn-status|vpn-extract] [ovpn_path] [output_dir]\n")
        return 1

    action = argv[0]
    ovpn_path = argv[1] if len(argv) > 1 else str(settings.DEFAULT_OVPN_PATH)

    if action == "vpn-extract":
        output_dir = argv[2] if len(argv) > 2 else None
        result = extract_ovpn_assets(ovpn_path, output_dir)
    else:
        commands: Dict[str, Callable[[str], Dict[str, object]]] = {
            "vpn-start": lambda path: start_vpn(path),
            "vpn-stop": lambda path: stop_vpn(path),
            "vpn-status": lambda path: vpn_status(path),
        }
        result = commands[action](ovpn_path)
    _print_json(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
