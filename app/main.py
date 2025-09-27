"""FastAPI app providing a minimal UI for on-demand UMG polling."""
from __future__ import annotations

import csv
import json
import logging
import math
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from . import settings
from .janitza_client import JanitzaUMG, REGISTER_UNITS, load_umg_config
from .poll import BACKGROUND_POLLER, poll_once
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


def _compute_cycles(total_minutes: float, interval_minutes: float) -> int:
    if interval_minutes <= 0:
        interval_minutes = 1
    return max(1, math.ceil(total_minutes / interval_minutes))


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
            "units": REGISTER_UNITS,
            "units_json": json.dumps(REGISTER_UNITS),
            "poller_running": BACKGROUND_POLLER.is_running(),
            "poller_last_payload": BACKGROUND_POLLER.last_payload,
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


@app.get("/poller")
async def poller_status() -> JSONResponse:
    payload = {
        "running": BACKGROUND_POLLER.is_running(),
        "last_payload": BACKGROUND_POLLER.last_payload,
        "last_error": BACKGROUND_POLLER.last_error,
    }
    return JSONResponse(content=payload)


@app.post("/start")
async def start_poll(interval: float = 1.0) -> JSONResponse:
    try:
        await run_in_threadpool(
            BACKGROUND_POLLER.start,
            interval_s=int(max(1, interval * 60)),
            cycles=None,
        )
    except RuntimeError as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})
    return JSONResponse(content={"status": "started", "interval_minutes": interval})


@app.post("/run-loop")
async def run_loop(minutes: float = 5.0, interval: float = 1.0) -> JSONResponse:
    cycles = _compute_cycles(minutes, interval)
    try:
        await run_in_threadpool(
            BACKGROUND_POLLER.start,
            interval_s=int(max(1, interval * 60)),
            cycles=cycles,
        )
    except RuntimeError as exc:
        return JSONResponse(status_code=409, content={"error": str(exc)})
    return JSONResponse(
        content={
            "status": "started",
            "interval_minutes": interval,
            "cycles": cycles,
            "total_minutes": minutes,
        }
    )


@app.post("/stop")
async def stop_poll() -> JSONResponse:
    stopped = await run_in_threadpool(BACKGROUND_POLLER.stop)
    status = "stopped" if stopped else "idle"
    return JSONResponse(content={"status": status})
