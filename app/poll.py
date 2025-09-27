"""Polling helpers for Janitza UMG via VPN."""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from typing import Callable, Dict, List, Optional

from .janitza_client import JanitzaUMG, load_umg_config
from .vpn_connection import VPNConnection

LOGGER = logging.getLogger(__name__)

_ESTIMATE_LOCK = Lock()
_runtime_estimate_s = 15.0


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _next_minute(dt: datetime) -> datetime:
    aligned = dt.replace(second=0, microsecond=0)
    return aligned + timedelta(minutes=1)


def _get_estimate() -> float:
    with _ESTIMATE_LOCK:
        return _runtime_estimate_s


def _update_estimate(duration_s: float) -> None:
    if duration_s <= 0:
        return
    with _ESTIMATE_LOCK:
        global _runtime_estimate_s
        smoothed = 0.7 * _runtime_estimate_s + 0.3 * duration_s
        _runtime_estimate_s = max(3.0, smoothed)


def _sleep_until(target_time: datetime, stop_event: Optional[Event] = None) -> None:
    while True:
        remaining = (target_time - _now()).total_seconds()
        if remaining <= 0:
            break
        wait_for = min(1.0, remaining)
        if stop_event:
            if stop_event.wait(wait_for):
                break
        else:
            time.sleep(wait_for)


def poll_once(target_time: Optional[datetime] = None) -> Dict[str, object]:
    """Ensure VPN is connected, read registers once, and disconnect if needed."""
    start_perf = time.perf_counter()
    vpn = VPNConnection()
    status_before = vpn.status()
    vpn_already_connected = bool(status_before.get("is_connected"))
    started_vpn = False

    if not vpn_already_connected:
        connect_result = vpn.connect()
        if not connect_result.get("is_connected"):
            raise RuntimeError("Unable to establish VPN tunnel before polling")
        started_vpn = True

    scheduled_time: Optional[datetime] = None
    if target_time is not None:
        scheduled_time = target_time.astimezone() if target_time.tzinfo else target_time.replace(tzinfo=timezone.utc).astimezone()

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

        if scheduled_time is not None:
            _sleep_until(scheduled_time)

        readings = client.read_registers()
        row, csv_path = client.export_csv(readings, timestamp_override=scheduled_time)
        payload = {
            "health": health,
            "data": row,
            "csv_path": str(csv_path),
        }
        return payload
    finally:
        duration = time.perf_counter() - start_perf
        _update_estimate(duration)
        if started_vpn:
            vpn.disconnect()


def poll_loop(
    interval_s: int = 60,
    cycles: Optional[int] = 1,
    *,
    callback: Optional[Callable[[Dict[str, object]], None]] = None,
    stop_event: Optional[Event] = None,
    align_to_minute: bool = False,
) -> None:
    interval_s = max(1, int(interval_s))
    executed = 0
    base_time: Optional[datetime] = None

    while cycles is None or executed < cycles:
        if stop_event and stop_event.is_set():
            break

        target_time: Optional[datetime] = None
        if align_to_minute:
            now = _now()
            if base_time is None:
                base_time = _next_minute(now)
            target_time = base_time + timedelta(seconds=interval_s * executed)
            target_time = target_time.replace(second=0, microsecond=0)
            wait_start = target_time - timedelta(seconds=_get_estimate())
            _sleep_until(wait_start, stop_event)
            if stop_event and stop_event.is_set():
                break

        payload = poll_once(target_time=target_time)

        if callback:
            callback(payload)
        else:
            print(json.dumps(payload, indent=2, sort_keys=True))

        executed += 1
        if cycles is not None and executed >= cycles:
            break

        if not align_to_minute:
            sleep_total = interval_s
            for _ in range(sleep_total):
                if stop_event and stop_event.is_set():
                    return
                time.sleep(1)


def aligned_poll_once(interval_s: int = 60) -> Dict[str, object]:
    results: List[Dict[str, object]] = []
    poll_loop(
        interval_s=interval_s,
        cycles=1,
        callback=results.append,
        align_to_minute=True,
    )
    if not results:
        raise RuntimeError("Polling cancelled")
    return results[0]


class BackgroundPoller:
    """Manage a background polling loop controlled via FastAPI endpoints."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._stop_event = Event()
        self._thread: Optional[Thread] = None
        self._align_to_minute: bool = True
        self.last_payload: Optional[Dict[str, object]] = None
        self.last_error: Optional[str] = None

    def start(self, *, interval_s: int = 60, cycles: Optional[int] = None, align_to_minute: bool = True) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("Polling already running")
            self._stop_event.clear()
            self.last_error = None
            self._align_to_minute = align_to_minute
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
                align_to_minute=self._align_to_minute,
            )
        except Exception as exc:  # pragma: no cover
            LOGGER.exception("Background polling failed: %s", exc)
            self.last_error = str(exc)
        finally:
            with self._lock:
                self._thread = None
            self._stop_event.clear()


BACKGROUND_POLLER = BackgroundPoller()
