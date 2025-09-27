"""Polling helpers for Janitza UMG via VPN."""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, Optional

from .janitza_client import JanitzaUMG, load_umg_config
from .vpn_connection import VPNConnection

LOGGER = logging.getLogger(__name__)


def poll_once() -> Dict[str, object]:
    """Ensure VPN is connected, read registers once, and disconnect if needed."""
    vpn = VPNConnection()
    status_before = vpn.status()
    vpn_already_connected = bool(status_before.get("is_connected"))
    started_vpn = False

    if not vpn_already_connected:
        connect_result = vpn.connect()
        if not connect_result.get("is_connected"):
            raise RuntimeError("Unable to establish VPN tunnel before polling")
        started_vpn = True

    try:
        umg_cfg = load_umg_config()
        client = JanitzaUMG(
            host=umg_cfg.get("host"),
            http_port=umg_cfg.get("http_port"),
            modbus_port=umg_cfg.get("modbus_port"),
            timeout_s=umg_cfg.get("timeout_s"),
            registers=umg_cfg.get("registers"),
        )

        health = client.health()
        if not health.get("reachable"):
            raise RuntimeError(f"UMG device unreachable: {json.dumps(health)}")

        readings = client.read_registers()
        row, csv_path = client.export_csv(readings)
        payload = {
            "health": health,
            "data": row,
            "csv_path": str(csv_path),
        }
        return payload
    finally:
        if started_vpn:
            vpn.disconnect()


def poll_loop(interval_s: int = 60, cycles: Optional[int] = 1) -> None:
    """Run ``poll_once`` repeatedly with a pause between cycles."""
    executed = 0
    while cycles is None or executed < cycles:
        payload = poll_once()
        print(json.dumps(payload, indent=2, sort_keys=True))
        executed += 1
        if cycles is not None and executed >= cycles:
            break
        time.sleep(max(1, interval_s))
