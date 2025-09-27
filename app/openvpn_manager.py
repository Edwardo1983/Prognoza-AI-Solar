"""Windows utilities for managing OpenVPN GUI sessions."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

import psutil

_LOGGER = logging.getLogger(__name__)

_OPENVPN_ENV_KEYS = ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"]
_CERT_EXTENSIONS = {".crt", ".key", ".pem"}


class OpenVPNManager:
    """Manage OpenVPN GUI profiles and processes on Windows."""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._logger = logger or _LOGGER
        self._cached_gui_path: Optional[Path] = None

    def find_openvpn_gui(self) -> Path:
        """Detect the ``openvpn-gui.exe`` binary and return its path."""
        if self._cached_gui_path and self._cached_gui_path.exists():
            return self._cached_gui_path

        candidates: list[Path] = []

        for key in _OPENVPN_ENV_KEYS:
            root = os.environ.get(key)
            if not root:
                continue
            candidate = Path(root) / "OpenVPN" / "bin" / "openvpn-gui.exe"
            candidates.append(candidate)

        path_env = os.environ.get("PATH", "")
        for entry in path_env.split(os.pathsep):
            if not entry:
                continue
            candidate = Path(entry).expanduser() / "openvpn-gui.exe"
            candidates.append(candidate)

        for candidate in candidates:
            try:
                if candidate.is_file():
                    self._cached_gui_path = candidate
                    self._logger.debug("openvpn-gui.exe detected at %s", candidate)
                    return candidate
            except OSError:
                continue

        raise FileNotFoundError(
            "openvpn-gui.exe not found. Install OpenVPN Community Edition from "
            "https://openvpn.net/community-downloads/ and verify the binary exists "
            "under C\\Program Files\\OpenVPN\\bin or C\\Program Files (x86)\\OpenVPN\\bin, "
            "or add it to the PATH environment variable."
        )

    def prepare_profile(self, clean_ovpn: Path, assets_dir: Path, profile_name: str) -> Path:
        """Copy the cleaned profile and certificate assets to the user config folder."""
        user_profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        config_dir = user_profile / "OpenVPN" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        destination = config_dir / f"{profile_name}.ovpn"
        shutil.copy2(clean_ovpn, destination)
        self._logger.debug("Copied clean profile to %s", destination)

        if assets_dir.is_dir():
            for asset in assets_dir.iterdir():
                if asset == clean_ovpn or not asset.is_file():
                    continue
                if asset.suffix.lower() not in _CERT_EXTENSIONS:
                    continue
                target = config_dir / asset.name
                shutil.copy2(asset, target)
                self._logger.debug("Copied asset %s to %s", asset, target)

        return destination

    def start(self, profile_name: str) -> Dict[str, Optional[int]]:
        """Launch the GUI connection for the provided profile."""
        gui_path = self.find_openvpn_gui()
        self._ensure_interactive_service()

        if self.is_running(profile_name):
            self._logger.info("Profile %s already active, reconnecting", profile_name)
            self.disconnect(profile_name)
            time.sleep(3)

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(  # noqa: S603
            [str(gui_path), "--connect", profile_name],
            creationflags=creation_flags,
        )
        self._logger.info("Issued connect command via %s (pid=%s)", gui_path, process.pid)

        time.sleep(2)
        pid = self._locate_profile_pid(profile_name)
        return {"pid": pid, "profile_name": profile_name}

    def disconnect(self, profile_name: str) -> None:
        """Disconnect a specific profile via the GUI helper."""
        gui_path = self.find_openvpn_gui()
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(  # noqa: S603
            [str(gui_path), "--command", "disconnect", profile_name],
            check=False,
            creationflags=creation_flags,
        )
        self._logger.info("Disconnect command sent for profile %s", profile_name)
        time.sleep(2)

        process = self._locate_profile_process(profile_name)
        if process:
            try:
                process.terminate()
                process.wait(timeout=10)
                self._logger.debug("Terminated lingering openvpn.exe pid=%s", process.pid)
            except (psutil.NoSuchProcess, psutil.TimeoutExpired):
                try:
                    process.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def stop_all(self) -> None:
        """Disconnect all sessions and close any lingering GUI instance."""
        gui_path = self.find_openvpn_gui()
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(  # noqa: S603
            [str(gui_path), "--command", "disconnect_all"],
            check=False,
            creationflags=creation_flags,
        )
        time.sleep(1)
        subprocess.run(  # noqa: S603
            [str(gui_path), "--command", "exit"],
            check=False,
            creationflags=creation_flags,
        )

        for process in psutil.process_iter(["name", "pid"]):
            try:
                if (process.info.get("name") or "").lower() == "openvpn.exe":
                    process.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    def is_running(self, profile_name: str) -> bool:
        """Determine whether the given profile has an active openvpn.exe process."""
        process = self._locate_profile_process(profile_name)
        return process is not None

    def _ensure_interactive_service(self) -> None:
        """Ensure the OpenVPN interactive service is running before GUI commands."""
        if os.name != "nt":
            return
        service_name = "OpenVPNServiceInteractive"
        try:
            service = psutil.win_service_get(service_name)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - platform specific
            self._logger.debug("Interactive service lookup failed: %s", exc)
            return
        try:
            status = service.status().lower()
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.debug("Interactive service status unavailable: %s", exc)
            return
        if status == "running":
            return
        self._logger.info("Starting %s service", service_name)
        result = subprocess.run(
            ["sc", "start", service_name],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if result.returncode != 0:
            message = (
                f"Failed to start {service_name}. Run the VPN orchestrator with administrative privileges "
                "or start the service manually from Services.msc. "
                f"Command output: {result.stderr.strip() or result.stdout.strip()}"
            )
            self._logger.error(message)
            raise RuntimeError(message)
        for _ in range(10):
            time.sleep(1)
            try:
                if service.status().lower() == "running":
                    self._logger.info("%s service is running", service_name)
                    return
            except Exception as exc:  # pragma: no cover - defensive
                self._logger.debug("Service status poll failed: %s", exc)
                break
        message = (
            f"Timed out waiting for {service_name} to report RUNNING. Verify the service is not disabled "
            "and that you have administrator rights."
        )
        self._logger.error(message)
        raise RuntimeError(message)

    def get_profile_pid(self, profile_name: str) -> Optional[int]:
        """Return the PID of the openvpn.exe process that serves ``profile_name``."""
        return self._locate_profile_pid(profile_name)

    def _locate_profile_pid(self, profile_name: str) -> Optional[int]:
        process = self._locate_profile_process(profile_name)
        return process.pid if process else None

    def _locate_profile_process(self, profile_name: str) -> Optional[psutil.Process]:
        profile_token = profile_name.lower()
        for process in psutil.process_iter(["name", "cmdline", "pid"]):
            try:
                name = (process.info.get("name") or "").lower()
                if "openvpn" not in name:
                    continue
                cmdline = " ".join(process.info.get("cmdline") or [])
                if profile_token in cmdline.lower():
                    return process
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None