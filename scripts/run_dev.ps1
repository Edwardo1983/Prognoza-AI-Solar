$ErrorActionPreference = "Stop"

python -m venv .venv
if (Test-Path .venv/Scripts/Activate.ps1) {
    . .venv/Scripts/Activate.ps1
} else {
    . .venv/bin/Activate.ps1
}
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python - <<'PY'
from app import vpn
from app import settings
result = vpn.vpn_status(str(settings.DEFAULT_OVPN_PATH))
print(result)
PY
