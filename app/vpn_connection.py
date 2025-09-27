"""High-level orchestration for establishing and monitoring the OpenVPN tunnel."""
from __future__ import annotations

import logging
import platform
import re
import socket
import subprocess
import time
from typing import Dict, Optional, Tuple

import psutil

from app import ovpn_config, settings
from app.openvpn_manager import OpenVPNManager

_LOGGER = logging.getLogger(__name__)


class VPNConnection:
    """Coordinate VPN profile preparation, connection, and health monitoring."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or _LOGGER
        self._manager = OpenVPNManager(self._logger)
        self._profile_name = settings.PROFILE_NAME
        self._last_status: Dict[str, object] = {}

    def connect(self) -> Dict[str, object]:
        """Establish the VPN tunnel and perform health checks."""
        start_time = time.monotonic()
        status: Dict[str, object] = {
            "is_connected": False,
            "vpn_ip": None,
            "umg_ok": False,
            "pid": None,
            "profile_name": self._profile_name,
            "log_path": str(settings.LOG_FILE),
            "checks": {},
        }

        try:
            parsed = ovpn_config.parse_ovpn_file(settings.OVPN_INPUT)
            original_text = str(parsed["text"])
            clean_text = ovpn_config.generate_clean_config(
                original_text,
                settings.OVPN_ASSETS_DIR,
                settings.UMG_IP,
                self._profile_name,
            )
            clean_profile = ovpn_config.write_clean_files(
                clean_text,
                settings.OVPN_ASSETS_DIR,
                self._profile_name,
            )
            profile_destination = self._manager.prepare_profile(
                clean_profile,
                settings.OVPN_ASSETS_DIR,
                self._profile_name,
            )
            self._logger.info("Prepared profile at %s", profile_destination)

            launch_info = self._manager.start(self._profile_name)
            status["pid"] = launch_info.get("pid")

            vpn_ip = self._wait_for_ip(settings.CONNECT_TIMEOUT_S)
            status["vpn_ip"] = vpn_ip
            if not vpn_ip:
                raise TimeoutError(
                    "Timed out waiting for TUN/TAP interface to obtain an IPv4 address."
                )

            success, ping_ok, tcp_ok = self._test_umg_connectivity(
                timeout_s=settings.CONNECT_TIMEOUT_S,
                min_attempts=3,
            )
            status["checks"] = {"ping": ping_ok, "tcp": tcp_ok}
            status["umg_ok"] = success
            status["is_connected"] = bool(vpn_ip and success)

            if not status["pid"]:
                status["pid"] = self._manager.get_profile_pid(self._profile_name)

            if status["is_connected"]:
                self._logger.info(
                    "VPN connected: ip=%s ping=%s tcp=%s pid=%s",
                    status["vpn_ip"],
                    ping_ok,
                    tcp_ok,
                    status["pid"],
                )
            else:
                self._logger.error(
                    "VPN health check failed: ping=%s tcp=%s", ping_ok, tcp_ok
                )

        except Exception as exc:  # noqa: BLE001
            self._logger.exception("Connection routine failed: %s", exc)
            status["error"] = str(exc)
        finally:
            status["elapsed_s"] = round(time.monotonic() - start_time, 2)
            self._last_status = status

        return status

    def disconnect(self) -> None:
        """Disconnect the OpenVPN profile and terminate lingering processes."""
        try:
            self._manager.disconnect(self._profile_name)
            self._manager.stop_all()
        finally:
            self._last_status = {
                "is_connected": False,
                "vpn_ip": None,
                "umg_ok": False,
                "pid": None,
                "profile_name": self._profile_name,
                "log_path": str(settings.LOG_FILE),
                "checks": {"ping": False, "tcp": False},
            }
            self._logger.info("Profile %s disconnected", self._profile_name)

    def status(self) -> Dict[str, object]:
        """Return the latest known state of the VPN tunnel."""
        pid = self._manager.get_profile_pid(self._profile_name)
        vpn_ip = self._get_vpn_ip()
        is_running = pid is not None
        success = False
        ping_ok = False
        tcp_ok = False

        if is_running and vpn_ip:
            success, ping_ok, tcp_ok = self._test_umg_connectivity(timeout_s=5, min_attempts=1)

        current = {
            "is_connected": bool(is_running and success and vpn_ip),
            "vpn_ip": vpn_ip,
            "umg_ok": bool(success),
            "pid": pid,
            "profile_name": self._profile_name,
            "log_path": str(settings.LOG_FILE),
            "checks": {"ping": ping_ok, "tcp": tcp_ok},
        }
        self._last_status = current
        return current

    def _wait_for_ip(self, timeout_s: int) -> Optional[str]:
        deadline = time.monotonic() + timeout_s
        delay = 2.0
        while time.monotonic() < deadline:
            vpn_ip = self._get_vpn_ip()
            if vpn_ip:
                self._logger.info("Obtained VPN interface IP %s", vpn_ip)
                return vpn_ip
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)
        return None

    def _test_umg_connectivity(self, timeout_s: float, min_attempts: int) -> Tuple[bool, bool, bool]:
        deadline = time.monotonic() + timeout_s
        attempts = 0
        delay = 2.0
        ping_ok = False
        tcp_ok = False

        while time.monotonic() < deadline or attempts < min_attempts:
            attempts += 1
            ping_ok = self._ping_host(settings.UMG_IP)
            tcp_ok = self._check_tcp(settings.UMG_IP, settings.UMG_TCP_PORT)
            self._logger.info(
                "UMG health attempt %s: ping=%s tcp=%s", attempts, ping_ok, tcp_ok
            )
            if ping_ok and tcp_ok:
                return True, ping_ok, tcp_ok
            if time.monotonic() >= deadline:
                break
            sleep_for = min(delay, max(0.5, deadline - time.monotonic()))
            time.sleep(sleep_for)
            delay = min(delay * 1.5, 10.0)

        return False, ping_ok, tcp_ok

    def _get_vpn_ip(self) -> Optional[str]:
        """Detect the IPv4 address of the TAP/TUN adapter using psutil only."""
        for iface, addrs in psutil.net_if_addrs().items():
            upper_iface = iface.upper()
            if "TAP" in upper_iface or "TUN" in upper_iface or "OPENVPN" in upper_iface:
                for addr in addrs:
                    if addr.family == socket.AF_INET:
                        ip_addr = getattr(addr, "address", None)
                        if ip_addr and not ip_addr.startswith(("0.", "169.254.")):
                            return ip_addr
        self._logger.warning("VPN IP not found - check if OpenVPN tunnel is up.")
        return None

    def _ping_host(self, host: str, timeout_ms: int = 1000) -> bool:
        count_flag = "-n" if platform.system() == "Windows" else "-c"
        command = ["ping", count_flag, "1", host]
        if platform.system() == "Windows":
            command.extend(["-w", str(timeout_ms)])
        else:
            command.extend(["-W", str(max(1, timeout_ms // 1000))])

        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            self._logger.debug("Ping %s failed: %s", host, result.stdout.strip())
        return result.returncode == 0

    def _check_tcp(self, host: str, port: int, timeout_s: float = 3.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return True
        except OSError as exc:
            self._logger.debug("TCP %s:%s failed: %s", host, port, exc)
            return False
