"""Client utilities for checking Janitza UMG connectivity."""
from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class JanitzaUMG:
    """Minimal connectivity checker for a Janitza UMG device."""

    host: str = "192.168.1.30"
    http_port: int = 80
    modbus_port: int = 502
    timeout_s: float = 3.0

    @staticmethod
    def tcp_ping(host: str, port: int, timeout_s: float) -> Optional[float]:
        """Attempt a TCP connection and return latency in milliseconds."""
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                end = time.perf_counter()
                return round((end - start) * 1000.0, 3)
        except (OSError, ValueError):
            return None

    def health(self) -> dict[str, Optional[float] | bool]:
        """Probe HTTP and Modbus endpoints, returning latency metrics."""
        http_ms = self.tcp_ping(self.host, self.http_port, self.timeout_s)
        modbus_ms = self.tcp_ping(self.host, self.modbus_port, self.timeout_s)
        reachable = modbus_ms is not None
        return {
            "http_ms": http_ms,
            "modbus_ms": modbus_ms,
            "reachable": reachable,
        }
