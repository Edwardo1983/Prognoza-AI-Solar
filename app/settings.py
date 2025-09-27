"""Application-wide settings for the VPN automation toolkit."""
from __future__ import annotations

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "app" / "data" / "raw"
LOG_FILE = DATA_DIR / "vpn.log"
SECRETS_DIR = BASE_DIR / "secrets"
OVPN_INPUT = SECRETS_DIR / "eduard.ordean@el-mont.ro-Brezoaia-PT.ovpn"
OVPN_ASSETS_DIR = SECRETS_DIR / "eduard.ordean@el-mont.ro-Brezoaia-PT_assets"
PROFILE_NAME = "eduard.ordean@el-mont.ro-Brezoaia-PT-clean"
UMG_IP = "192.168.1.30"
UMG_TCP_PORT = 502
CONNECT_TIMEOUT_S = 90

for directory in (DATA_DIR, OVPN_ASSETS_DIR):
    directory.mkdir(parents=True, exist_ok=True)
