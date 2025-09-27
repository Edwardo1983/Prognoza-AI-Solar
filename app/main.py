"""FastAPI app providing a minimal UI for on-demand UMG polling."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import settings
from .janitza_client import JanitzaUMG, load_umg_config
from .poll import poll_once
from .vpn_connection import VPNConnection

LOGGER = logging.getLogger(__name__)
app = FastAPI(title="UMG On-Demand Poller")
TEMPLATES = Jinja2Templates(directory=str(settings.BASE_DIR / "app" / "templates"))


def _get_latest_csv() -> Optional[tuple[Path, Dict[str, str]]]:
    files = sorted(settings.EXPORTS_DIR.glob("umg_readings_*.csv"))
    if not files:
        return None
    latest = files[-1]
    try:
        with latest.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            row: Optional[Dict[str, str]] = None
            for row in reader:
                pass
    except FileNotFoundError:
        return None
    if not row:
        return None
    return latest, row


def _vpn_status() -> Dict[str, object]:
    connection = VPNConnection()
    return connection.status()


def _umg_health() -> Dict[str, object]:
    cfg = load_umg_config()
    client = JanitzaUMG(
        host=cfg.get("host"),
        http_port=cfg.get("http_port"),
        modbus_port=cfg.get("modbus_port"),
        timeout_s=cfg.get("timeout_s"),
        registers=cfg.get("registers"),
    )
    return client.health()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    vpn_status = await run_in_threadpool(_vpn_status)
    health = await run_in_threadpool(_umg_health)
    latest = await run_in_threadpool(_get_latest_csv)
    latest_row = None
    latest_file = None
    if latest:
        latest_file, latest_row = latest
    return TEMPLATES.TemplateResponse(
        "index.html",
        {
            "request": request,
            "vpn_status": vpn_status,
            "health": health,
            "latest_row": latest_row,
            "latest_file": str(latest_file) if latest_file else None,
        },
    )


@app.post("/run")
async def run_poll() -> JSONResponse:
    try:
        result = await run_in_threadpool(poll_once)
    except Exception as exc:  # pragma: no cover - runtime errors
        LOGGER.exception("Polling failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})
    return JSONResponse(content=result)


@app.get("/health")
async def health_endpoint() -> JSONResponse:
    try:
        health = await run_in_threadpool(_umg_health)
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Health probe failed: %s", exc)
        return JSONResponse(status_code=500, content={"error": str(exc)})
    return JSONResponse(content=health)


@app.get("/status")
async def status_endpoint() -> JSONResponse:
    status = await run_in_threadpool(_vpn_status)
    return JSONResponse(content=status)
