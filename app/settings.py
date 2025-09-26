"""Application settings and filesystem bootstrap for Prognoza AI Solar."""
from __future__ import annotations

from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DATA_DIR = DATA_DIR / "raw"
EXPORTS_DIR = DATA_DIR / "exports"
SECRETS_DIR = BASE_DIR / "secrets"

# Ensure required directories exist at import time.
for directory in (DATA_DIR, RAW_DATA_DIR, EXPORTS_DIR, SECRETS_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# Default location of the OpenVPN configuration file.
DEFAULT_OVPN_PATH = SECRETS_DIR / "Prognoza-UMG-509-PRO.ovpn"

# Runtime files for CLI management.
VPN_LOG_FILE = RAW_DATA_DIR / "vpn.log"
VPN_PID_FILE = RAW_DATA_DIR / "vpn.pid"

# Constants for health checks.
VPN_HEALTH_HOST = "192.168.1.30"
VPN_HEALTH_PORT = 80
