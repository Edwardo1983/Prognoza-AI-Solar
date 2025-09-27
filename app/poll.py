"""Polling helpers for Janitza UMG via VPN."""
from __future__ import annotations

import json
import logging
import math
import time
from threading import Event, Lock, Thread
from typing import Callable, Dict, Optional

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


def poll_loop(
    interval_s: int = 60,
    cycles: Optional[int] = 1,
    *,
    callback: Optional[Callable[[Dict[str, object]], None]] = None,
    stop_event: Optional[Event] = None,
) -> None:
    """Run ``poll_once`` repeatedly with a pause between cycles."""
    executed = 0
    while cycles is None or executed < cycles:
        if stop_event and stop_event.is_set():
            break
        payload = poll_once()
        if callback:
            callback(payload)
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))
        executed += 1
        if cycles is not None and executed >= cycles:
            break
        sleep_target = max(1, interval_s)
        for _ in range(sleep_target):
            if stop_event and stop_event.is_set():
                return
            time.sleep(1)


class BackgroundPoller:
    """Manage a background polling loop controlled via FastAPI endpoints."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self.last_payload: Optional[Dict[str, object]] = None
        self.last_error: Optional[str] = None

    def start(self, *, interval_s: int = 60, cycles: Optional[int] = None) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Polling already running")
            self._stop_event.clear()
            self.last_error = None
            thread = Thread(
                target=self._run,
                args=(interval_s, cycles),
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> bool:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return False
            self._stop_event.set()
            thread = self._thread
        thread.join(timeout=5)
        with self._lock:
            self._thread = None
        return True

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def _store_payload(self, payload: Dict[str, object]) -> None:
        self.last_payload = payload

    def _run(self, interval_s: int, cycles: Optional[int]) -> None:
        try:
            poll_loop(
                interval_s=interval_s,
                cycles=cycles,
                callback=self._store_payload,
                stop_event=self._stop_event,
            )
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Background polling failed: %s", exc)
            self.last_error = str(exc)
        finally:
            with self._lock:
                self._thread = None
            self._stop_event.clear()


BACKGROUND_POLLER = BackgroundPoller()
