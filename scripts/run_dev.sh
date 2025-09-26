#!/usr/bin/env bash
set -euo pipefail

python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python - <<'PY'
from app import vpn
from app import settings
result = vpn.vpn_status(str(settings.DEFAULT_OVPN_PATH))
print(result)
PY
