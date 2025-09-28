"""Client utilities for checking and reading Janitza UMG values."""
from __future__ import annotations

import json
import logging
import math
import socket
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
import yaml
from pymodbus.client import ModbusTcpClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

from . import settings

LOGGER = logging.getLogger(__name__)

DEFAULT_REGISTERS: Dict[str, int] = {
    "power_active_total": 19026,
    "power_reactive_total": 19042,
    "power_apparent_total": 19034,
    "energy_active_import": 19062,
    "energy_active_export": 19070,
    "energy_reactive_import": 19094,
    "energy_reactive_export": 19102,
    "voltage_l1": 19000,
    "voltage_l2": 19002,
    "voltage_l3": 19004,
    "current_l1": 19012,
    "current_l2": 19014,
    "current_l3": 19016,
    "frequency": 19050,
    "power_factor": 19636,
    "thd_voltage_l1": 19110,
    "thd_current_l1": 19116,
}

REGISTER_UNITS: Dict[str, str] = {
    "power_active_total": "W",
    "power_reactive_total": "var",
    "power_apparent_total": "VA",
    "energy_active_import": "Wh",
    "energy_active_export": "Wh",
    "energy_reactive_import": "varh",
    "energy_reactive_export": "varh",
    "voltage_l1": "V",
    "voltage_l2": "V",
    "voltage_l3": "V",
    "current_l1": "A",
    "current_l2": "A",
    "current_l3": "A",
    "frequency": "Hz",
    "power_factor": "PF",
    "thd_voltage_l1": "%",
    "thd_current_l1": "%",
}


@dataclass
class JanitzaUMG:
    """Minimal Janitza UMG helper supporting connectivity and Modbus reads."""

    host: str = settings.UMG_IP
    http_port: int = 80
    modbus_port: int = settings.UMG_TCP_PORT
    timeout_s: float = 3.0
    registers: Dict[str, int] | None = None
    unit_id: int = 1

    def __post_init__(self) -> None:
        if self.registers is None:
            self.registers = DEFAULT_REGISTERS.copy()

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

    def health(self) -> Dict[str, Optional[float] | bool]:
        """Probe HTTP and Modbus ports returning latency metrics."""
        http_ms = self.tcp_ping(self.host, self.http_port, self.timeout_s)
        modbus_ms = self.tcp_ping(self.host, self.modbus_port, self.timeout_s)
        reachable = modbus_ms is not None
        return {
            "http_ms": http_ms,
            "modbus_ms": modbus_ms,
            "reachable": reachable,
        }

    def read_registers(self) -> Dict[str, Optional[float]]:
        """Read configured holding registers as IEEE-754 floats."""
        client = ModbusTcpClient(host=self.host, port=self.modbus_port, timeout=self.timeout_s)
        if not client.connect():
            raise ConnectionError(f"Unable to establish Modbus TCP session with {self.host}:{self.modbus_port}")

        results: Dict[str, Optional[float]] = {}
        try:
            for name, address in self.registers.items():
                value = self._read_float(client, address)
                results[name] = value
        finally:
            client.close()
        return results
    
    def read_registers(self, retries: int = 3) -> Dict[str, Optional[float]]:
        """Read configured holding registers as IEEE-754 floats with retries."""
        attempts = max(1, retries)
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            client = ModbusTcpClient(host=self.host, port=self.modbus_port, timeout=self.timeout_s)
            try:
                if not client.connect():
                    raise ConnectionError(
                        f"Unable to establish Modbus TCP session with {self.host}:{self.modbus_port}"
                    )
    
                results: Dict[str, Optional[float]] = {}
                for name, address in self.registers.items():
                    value: Optional[float] = None
                    for _ in range(2):
                        value = self._read_float(client, address)
                        if value is not None:
                            break
                        time.sleep(0.2)
                    results[name] = value
                return results
            except Exception as exc:  # pragma: no cover - network resilience
                LOGGER.warning("Modbus read attempt %s/%s failed: %s", attempt + 1, attempts, exc)
                last_error = exc
                time.sleep(1)
            finally:
                try:
                    client.close()
                except Exception:
                    pass
        raise ConnectionError(last_error or RuntimeError("Modbus read exhausted retries"))
    
    

    def _read_float(self, client: ModbusTcpClient, address: int) -> Optional[float]:
        try:
            response = client.read_holding_registers(address=address, count=2, slave=self.unit_id)
        except Exception as exc:  # pragma: no cover - network failure
            LOGGER.debug("Modbus read failed for %s @ %s: %s", self.host, address, exc)
            return None
        if not response or getattr(response, "isError", lambda: True)():
            LOGGER.debug("Modbus read error for address %s", address)
            return None
        registers = getattr(response, "registers", None)
        if not registers or len(registers) != 2:
            return None
        decoder = BinaryPayloadDecoder.fromRegisters(
            registers,
            byteorder=Endian.BIG,
            wordorder=Endian.BIG,
        )
        try:
            value = decoder.decode_32bit_float()
        except Exception:  # pragma: no cover - malformed payload
            return None
        if math.isnan(value) or math.isinf(value):
            return None
        return float(np.float32(value))


    def export_csv(
        self,
        values: Dict[str, Optional[float]],
        path: Optional[Path] = None,
        timestamp_override: Optional[datetime] = None,
    ) -> Tuple[Dict[str, object], Path]:
        """Append readings to a daily CSV and return the stored row."""
        timestamp = timestamp_override or datetime.now(timezone.utc).astimezone()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc).astimezone()
        else:
            timestamp = timestamp.astimezone()
        timestamp_str = timestamp.isoformat()

        exports_dir = Path(path) if path else settings.EXPORTS_DIR
        exports_dir.mkdir(parents=True, exist_ok=True)
        csv_path = exports_dir / f"umg_readings_{timestamp.date().isoformat()}.csv"

        if csv_path.exists():
            try:
                head = pd.read_csv(csv_path, usecols=["timestamp"], nrows=1)
                first_raw = str(head.iloc[0]["timestamp"])
                first_ts = datetime.fromisoformat(first_raw)
                if first_ts.tzinfo is None:
                    first_ts = first_ts.replace(tzinfo=timezone.utc).astimezone(timestamp.tzinfo)
                else:
                    first_ts = first_ts.astimezone(timestamp.tzinfo)
            except Exception:  # pragma: no cover
                first_ts = timestamp
        else:
            first_ts = timestamp

        elapsed_minutes = round((timestamp - first_ts).total_seconds() / 60.0, 2)
        thresholds = [5, 10, 15, 30, 60]
        milestones = ";".join(str(t) for t in thresholds if elapsed_minutes >= t)

        row: Dict[str, object] = {
            "timestamp": timestamp_str,
            **values,
            "elapsed_minutes": elapsed_minutes,
            "milestones": milestones,
        }

        pd.DataFrame([row]).to_csv(
            csv_path,
            mode="a",
            header=not csv_path.exists(),
            index=False,
        )
        return row, csv_path



def load_umg_config() -> Dict[str, object]:
    """Load UMG settings from config.yaml, falling back to defaults."""
    if not settings.CONFIG_FILE.exists():
        return {"registers": DEFAULT_REGISTERS.copy()}
    with settings.CONFIG_FILE.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    umg_cfg = data.get("umg", {})
    registers = umg_cfg.get("registers") or DEFAULT_REGISTERS.copy()
    return {
        "host": umg_cfg.get("host", settings.UMG_IP),
        "http_port": umg_cfg.get("http_port", 80),
        "modbus_port": umg_cfg.get("modbus_port", settings.UMG_TCP_PORT),
        "timeout_s": umg_cfg.get("timeout_s", 3),
        "registers": registers,
    }
