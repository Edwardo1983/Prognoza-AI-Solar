"""Utilities for managing an OpenVPN tunnel on demand."""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

import psutil
import shutil

from app import settings

OPENVPN_HOST = settings.VPN_HEALTH_HOST
OPENVPN_PORT = settings.VPN_HEALTH_PORT

LOG_TAIL_BYTES = 65_536


@dataclass
class _CliState:
    """Snapshot of the CLI runtime state and recent log analysis."""

    pid: Optional[int]
    process_running: bool
    connected: bool
    reachable: bool
    last_error: Optional[str]
    last_log_line: Optional[str]


def _find_openvpn_gui() -> Optional[Path]:
    """Attempt to locate the OpenVPN GUI executable on Windows systems."""
    candidates = []
    env_keys = ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"]
    for key in env_keys:
        base = os.environ.get(key)
        if base:
            candidates.append(Path(base) / "OpenVPN" / "bin" / "OpenVPNGUI.exe")
    for path_entry in os.environ.get("PATH", "").split(os.pathsep):
        if path_entry:
            candidates.append(Path(path_entry) / "OpenVPNGUI.exe")

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _find_openvpn_cli() -> Optional[Path]:
    """Locate the OpenVPN CLI binary using the current PATH."""
    which = shutil.which("openvpn")
    if which:
        return Path(which).resolve()
    return None


def find_openvpn() -> Dict[str, Optional[Path]]:
    """Detect available OpenVPN entry points.

    Returns
    -------
    dict
        Dictionary with keys ``method`` (``"gui"``, ``"cli"``, or ``"missing"``),
        ``gui_path``, and ``cli_path`` representing resolved executable paths.
    """
    gui_path: Optional[Path] = None
    cli_path: Optional[Path] = None

    if platform.system().lower() == "windows":
        gui_path = _find_openvpn_gui()
    cli_path = _find_openvpn_cli()

    if gui_path:
        method = "gui"
    elif cli_path:
        method = "cli"
    else:
        method = "missing"

    return {"method": method, "gui_path": gui_path, "cli_path": cli_path}


def _profile_name(ovpn_path: Path) -> str:
    return ovpn_path.stem


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check whether a TCP connection can be established."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _is_process_alive(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


def _read_log_tail(max_bytes: int = LOG_TAIL_BYTES) -> list[str]:
    """Return the tail of the OpenVPN log to avoid loading large files."""

    log_path = settings.VPN_LOG_FILE
    if not log_path.exists():
        return []

    try:
        with log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if size <= max_bytes:
                handle.seek(0)
            else:
                handle.seek(-max_bytes, os.SEEK_END)
            data = handle.read()
    except OSError:
        return []

    return data.decode("utf-8", errors="ignore").splitlines()


def _interpret_log(lines: list[str]) -> tuple[bool, Optional[str], Optional[str]]:
    """Inspect log lines looking for success or well-known failure messages."""

    if not lines:
        return False, None, None

    failure_patterns = [
        ("auth_failed", "Authentication failed"),
        ("cannot open tun/tap", "Cannot open tunnel interface"),
        ("permission denied", "Permission denied while configuring tunnel"),
        ("exiting due to fatal error", "OpenVPN exited due to a fatal error"),
        ("connection reset", "Connection reset by peer"),
        ("tls error", "TLS handshake error"),
        ("sigterm", "Session terminated"),
    ]

    last_line: Optional[str] = None
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        last_line = line if last_line is None else last_line
        lowered = line.lower()
        if "initialization sequence completed" in lowered:
            return True, None, last_line
        for pattern, message in failure_patterns:
            if pattern in lowered:
                return False, f"{message}: {line}", last_line

    return False, None, last_line


def _build_cli_state(expected_pid: Optional[int] = None) -> _CliState:
    """Gather runtime state for the CLI based tunnel."""

    pid = expected_pid or _read_pid_file()
    process_running = bool(pid and _is_process_alive(pid))
    if not process_running:
        pid = None
        _remove_pid_file()

    connected_from_log, failure_reason, last_log_line = _interpret_log(_read_log_tail())
    connected = process_running and connected_from_log
    reachable = connected and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)

    return _CliState(
        pid=pid,
        process_running=process_running,
        connected=connected,
        reachable=reachable,
        last_error=failure_reason,
        last_log_line=last_log_line,
    )


def _find_openvpn_process_for_profile(profile: str) -> Optional[psutil.Process]:
    profile_lower = profile.lower()
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "openvpn" in name or "openvpn" in cmdline:
            if profile_lower in cmdline or profile_lower in name:
                return proc
    return None


def _write_pid_file(pid: int) -> None:
    settings.VPN_PID_FILE.write_text(str(pid), encoding="utf-8")


def _read_pid_file() -> Optional[int]:
    if not settings.VPN_PID_FILE.exists():
        return None
    try:
        return int(settings.VPN_PID_FILE.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _remove_pid_file() -> None:
    if settings.VPN_PID_FILE.exists():
        try:
            settings.VPN_PID_FILE.unlink()
        except OSError:
            pass


def _spawn_openvpn_cli(ovpn_path: Path, cli_path: Path) -> Optional[int]:
    """Launch the OpenVPN CLI in a detached subprocess."""
    creationflags = 0
    preexec_fn = None
    if os.name == "nt":  # pragma: win32-no-cover
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        preexec_fn = os.setsid  # type: ignore[attr-defined]

    settings.VPN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    settings.VPN_LOG_FILE.touch(exist_ok=True)

    with settings.VPN_LOG_FILE.open("ab") as log_handle:
        process = subprocess.Popen(  # noqa: S603,S607
            [str(cli_path), "--config", str(ovpn_path)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            preexec_fn=preexec_fn,
            close_fds=True,
        )
    _write_pid_file(process.pid)
    return process.pid


def start_vpn(ovpn_path: str, start_timeout_s: int = 25) -> Dict[str, object]:
    """Start an OpenVPN tunnel if possible."""
    ovpn_file = Path(ovpn_path)
    detection = find_openvpn()
    profile = _profile_name(ovpn_file)

    if not ovpn_file.exists():
        return {
            "running": False,
            "reachable": False,
            "method": detection["method"],
            "pid": None,
            "message": f"Configuration file not found: {ovpn_file}",
        }

    if detection["method"] == "missing":
        return {
            "running": False,
            "reachable": False,
            "method": "missing",
            "pid": None,
            "message": "OpenVPN executable not found. Install OpenVPN GUI or CLI.",
        }

    deadline = time.time() + start_timeout_s

    if detection["method"] == "gui" and detection["gui_path"]:
        command = [str(detection["gui_path"]), "--command", "connect", profile]
        subprocess.run(command, check=False)  # noqa: S603

        while time.time() < deadline:
            time.sleep(1)
            process = _find_openvpn_process_for_profile(profile)
            pid = process.pid if process else None
            running = process is not None
            reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
            if running and reachable:
                return {
                    "running": True,
                    "reachable": True,
                    "method": "gui",
                    "pid": pid,
                    "message": "VPN connected via GUI.",
                }

        process = _find_openvpn_process_for_profile(profile)
        pid = process.pid if process else None
        running = process is not None
        reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
        if running and not reachable:
            message = (
                "OpenVPN GUI session detected but VPN health check host"
                f" {OPENVPN_HOST}:{OPENVPN_PORT} is unreachable."
            )
        else:
            message = "OpenVPN GUI did not report a reachable connection in time."

        return {
            "running": running,
            "reachable": reachable,
            "method": "gui",
            "pid": pid,
            "message": message,
        }

    if detection["method"] == "cli" and detection["cli_path"]:
        state = _build_cli_state()
        if not state.process_running:
            pid = _spawn_openvpn_cli(ovpn_file, detection["cli_path"])
            state = _build_cli_state(pid)

        while time.time() < deadline:
            time.sleep(1)
            state = _build_cli_state(state.pid)
            if state.connected:
                message = (
                    "VPN connected via CLI."
                    if state.reachable
                    else (
                        "VPN connected via CLI but health check host"
                        f" {OPENVPN_HOST}:{OPENVPN_PORT} is unreachable."
                    )
                )
                return {
                    "running": True,
                    "reachable": state.reachable,
                    "method": "cli",
                    "pid": state.pid,
                    "message": message,
                }

            if not state.process_running:
                break

        # Timeout or unexpected exit
        if state.process_running and not state.connected:
            message = "Timed out waiting for OpenVPN CLI to complete the handshake."
            if state.last_log_line:
                message += f" Last log line: {state.last_log_line}"
            return {
                "running": False,
                "reachable": state.reachable,
                "method": "cli",
                "pid": state.pid,
                "message": message,
            }

        failure_message = state.last_error or "OpenVPN CLI process terminated before establishing the tunnel."
        if state.last_log_line and state.last_log_line not in failure_message:
            failure_message = f"{failure_message} Last log line: {state.last_log_line}"

        return {
            "running": False,
            "reachable": state.reachable,
            "method": "cli",
            "pid": state.pid,
            "message": failure_message,
        }

    return {
        "running": False,
        "reachable": False,
        "method": detection["method"],
        "pid": None,
        "message": "Unable to determine how to start OpenVPN.",
    }


def stop_vpn(ovpn_path: str) -> Dict[str, object]:
    """Stop the OpenVPN tunnel if it is running."""
    ovpn_file = Path(ovpn_path)
    detection = find_openvpn()
    profile = _profile_name(ovpn_file)

    if detection["method"] == "gui" and detection["gui_path"]:
        command = [str(detection["gui_path"]), "--command", "disconnect", profile]
        subprocess.run(command, check=False)  # noqa: S603
        time.sleep(2)
        process = _find_openvpn_process_for_profile(profile)
        running = process is not None
        pid = process.pid if process else None
        reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
        if running:
            message = "OpenVPN GUI still reports a running session after disconnect command."
        else:
            message = "Disconnect command issued via GUI."
        return {
            "running": running,
            "reachable": reachable,
            "method": "gui",
            "pid": pid,
            "message": message,
        }
    elif detection["method"] == "cli":
        state = _build_cli_state()
        if not state.process_running:
            message = state.last_error or "OpenVPN CLI does not appear to be running."
            if state.last_log_line and state.last_log_line not in (state.last_error or ""):
                message = f"{message} Last log line: {state.last_log_line}"
            return {
                "running": False,
                "reachable": state.reachable,
                "method": "cli",
                "pid": None,
                "message": message,
            }

        try:
            process = psutil.Process(state.pid) if state.pid else None
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired:
                    process.kill()
        except psutil.NoSuchProcess:
            pass

        _remove_pid_file()
        new_state = _build_cli_state()
        message = "VPN CLI process terminated"
        return {
            "running": False,
            "reachable": new_state.reachable,
            "method": "cli",
            "pid": None,
            "message": message,
        }
    else:
        return {
            "running": False,
            "reachable": False,
            "method": "missing",
            "pid": None,
            "message": "OpenVPN executable not found.",
        }


def vpn_status(ovpn_path: str) -> Dict[str, object]:
    """Return the current status of the VPN connection."""
    ovpn_file = Path(ovpn_path)
    detection = find_openvpn()
    profile = _profile_name(ovpn_file)

    pid: Optional[int] = None
    running = False

    if detection["method"] == "gui":
        process = _find_openvpn_process_for_profile(profile)
        running = process is not None
        pid = process.pid if process else None
        reachable = running and _tcp_reachable(OPENVPN_HOST, OPENVPN_PORT)
        if running and reachable:
            message = "VPN connected via GUI."
        elif running:
            message = (
                "OpenVPN GUI process detected but VPN health check host"
                f" {OPENVPN_HOST}:{OPENVPN_PORT} is unreachable."
            )
        else:
            message = "VPN not connected."
        return {
            "running": running,
            "reachable": reachable,
            "method": detection["method"],
            "pid": pid,
            "message": message,
        }

    if detection["method"] == "cli":
        state = _build_cli_state()
        if state.connected:
            message = (
                "VPN connected via CLI."
                if state.reachable
                else (
                    "VPN connected via CLI but health check host"
                    f" {OPENVPN_HOST}:{OPENVPN_PORT} is unreachable."
                )
            )
        else:
            message = state.last_error or "VPN not connected."
            if state.last_log_line and state.last_log_line not in (state.last_error or ""):
                message = f"{message} Last log line: {state.last_log_line}"

        return {
            "running": state.connected,
            "reachable": state.reachable,
            "method": detection["method"],
            "pid": state.pid,
            "message": message,
        }

    return {
        "running": False,
        "reachable": False,
        "method": detection["method"],
        "pid": None,
        "message": "OpenVPN executable not found.",
    }
