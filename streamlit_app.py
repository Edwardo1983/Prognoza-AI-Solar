import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import plotly.express as px
import streamlit as st

from app import settings
from app.janitza_client import REGISTER_UNITS, JanitzaUMG, load_umg_config
from app.poll import BACKGROUND_POLLER
from app.vpn_connection import VPNConnection

st.set_page_config(page_title="UMG Streamlit Control", layout="wide")
logger = logging.getLogger(__name__)
AUTO_REFRESH_SECONDS = 300
now_ts = datetime.now().timestamp()
next_refresh_key = "next_refresh_ts"
if next_refresh_key not in st.session_state:
    st.session_state[next_refresh_key] = now_ts + AUTO_REFRESH_SECONDS
elif now_ts >= st.session_state[next_refresh_key]:
    st.session_state[next_refresh_key] = now_ts + AUTO_REFRESH_SECONDS
    st.experimental_rerun()


def reset_auto_refresh():
    st.session_state[next_refresh_key] = datetime.now().timestamp() + AUTO_REFRESH_SECONDS


ROBOTO_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap');
html, body, [class*="css"]  { font-family: 'Roboto', sans-serif; }
button, .stButton>button {
  font-family: 'Roboto', sans-serif;
  padding: 0.6rem 1.4rem;
  font-size: 1.05rem;
  border-radius: 12px;
  border: none;
  box-shadow: 0 6px 0 rgba(0,0,0,0.2);
}
button:active, .stButton>button:active { box-shadow: 0 2px 0 rgba(0,0,0,0.2); transform: translateY(2px); }
.run-btn { background: linear-gradient(145deg,#76ff7a,#2ecc71); color: #063; }
.stop-btn { background: linear-gradient(145deg,#ff6b6b,#c0392b); color: #fff; }
.health-btn { background: linear-gradient(145deg,#ffd86b,#f1c40f); color: #5c4300; }
.metric-header { font-weight: 600; font-size: 1.1rem; }
.status-badge { padding: 0.2rem 0.6rem; border-radius: 999px; font-weight: 600; }
.status-ok { background: #d4f8d4; color: #18643f; }
.status-partial { background: #fff3cd; color: #856404; }
.status-error { background: #f8d7da; color: #721c24; }
.signal-bar { display:inline-block; width:10px; height:24px; margin-right:4px; border-radius:3px; background:#e0e0e0; }
.signal-bar.on { background:#27ae60; }
.signal-bar.off { background:#e74c3c; }
</style>
"""

st.markdown(ROBOTO_CSS, unsafe_allow_html=True)

CSV_GLOB = "umg_readings_*.csv"

METRIC_ORDER = [
    "power_active_total",
    "power_reactive_total",
    "power_apparent_total",
    "energy_active_import",
    "energy_active_export",
    "energy_reactive_import",
    "energy_reactive_export",
    "voltage_l1",
    "voltage_l2",
    "voltage_l3",
    "current_l1",
    "current_l2",
    "current_l3",
    "frequency",
    "power_factor",
    "thd_voltage_l1",
    "thd_current_l1",
]

DISPLAY_NAMES = {key: key.replace('_', ' ').title() for key in METRIC_ORDER}
DISPLAY_NAMES.update({
    "timestamp": "Date / Time",
    "status": "Status",
    "error": "Error",
    "offset_seconds": "Offset Seconds",
})


def read_daily_data(date: datetime | None = None) -> pd.DataFrame:
    date = date or datetime.now().date()
    csv_path = settings.EXPORTS_DIR / f"umg_readings_{date.isoformat()}.csv"
    if not csv_path.exists():
        files = sorted(settings.EXPORTS_DIR.glob(CSV_GLOB))
        if not files:
            return pd.DataFrame()
        csv_path = files[-1]
    attempts = 5
    for attempt in range(1, attempts + 1):
        try:
            df = pd.read_csv(csv_path)
            break
        except (PermissionError, pd.errors.EmptyDataError, OSError) as exc:
            logger.warning(
                "Failed to read %s on attempt %s/%s: %s",
                csv_path,
                attempt,
                attempts,
                exc,
            )
            if attempt == attempts:
                logger.exception("Giving up reading %s after repeated errors", csv_path)
                st.warning(
                    f"Could not read data from {csv_path.name}. Displaying empty dataset.",
                    icon="⚠️",
                )
                return pd.DataFrame()
            time.sleep(0.2 * attempt)
    if df.empty:
        return df
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc

    try:
        timestamps = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    except Exception:
        logger.exception("Failed bulk conversion of timestamps in %s; falling back to per-row parsing", csv_path)

        def _safe_parse(value):
            if pd.isna(value):
                return pd.NaT
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    return value.replace(tzinfo=timezone.utc)
                return value.astimezone(timezone.utc)
            if isinstance(value, (int, float)):
                try:
                    return datetime.fromtimestamp(value, tz=timezone.utc)
                except OSError:
                    return pd.NaT
            value_str = str(value).strip()
            if not value_str:
                return pd.NaT
            for fmt in (
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S%z",
                "%d-%m-%YT%H:%M:%S.%f%z",
                "%d-%m-%YT%H:%M:%S%z",
                "%d.%m.%Y %H:%M:%S%z",
            ):
                try:
                    return datetime.strptime(value_str, fmt).astimezone(timezone.utc)
                except ValueError:
                    continue
            parsed = pd.to_datetime(value_str, utc=True, errors="coerce")
            if isinstance(parsed, pd.Timestamp) and not pd.isna(parsed):
                return parsed.to_pydatetime()
            return pd.NaT

        timestamps = pd.Series([_safe_parse(val) for val in df["timestamp"]], dtype="datetime64[ns, UTC]")

    df["timestamp"] = timestamps.dt.tz_convert(local_tz)
    if "status" not in df.columns:
        df["status"] = "ok"
    if "error" not in df.columns:
        df["error"] = ""
    if "offset_seconds" not in df.columns:
        df["offset_seconds"] = 0.0
    return df.sort_values("timestamp").reset_index(drop=True)


def human_metric(key: str) -> str:
    return DISPLAY_NAMES.get(key, key.replace('_', ' ').title())


def build_left_table(row: pd.Series) -> pd.DataFrame:
    rows: List[Dict[str, str]] = []
    timestamp: datetime = row["timestamp"]
    offset = row.get("offset_seconds", 0.0)
    status = row.get("status", "ok")
    status_badge = status
    rows.append({
        "Metric": "Date / Time",
        "Value": f"{timestamp:%d.%m.%Y | %H:%M:%S.%f}"[:-3] + f" | {offset:+.2f} s",
        "Units": status_badge,
    })
    for metric in METRIC_ORDER:
        if metric not in row:
            continue
        value = row.get(metric)
        unit = REGISTER_UNITS.get(metric, "")
        rows.append({
            "Metric": human_metric(metric),
            "Value": "" if pd.isna(value) else f"{value}",
            "Units": unit,
        })
    return pd.DataFrame(rows)


def style_left_table(df: pd.DataFrame, status: str):
    def highlight(row):
        if status == "error":
            return ["background-color: #f8d7da; color:#721c24" for _ in row]
        if status == "partial":
            return ["background-color: #fff3cd; color:#856404" for _ in row]
        return ["" for _ in row]
    return df.style.apply(highlight, axis=1)


def collect_hour_options(df: pd.DataFrame) -> List[str]:
    if df.empty:
        return ["Now"]
    tz = df["timestamp"].dt.tz
    hours = sorted({ts.replace(minute=0, second=0, microsecond=0) for ts in df["timestamp"]})
    formatted = [ts.strftime("%H:00") for ts in hours]
    return ["Now"] + formatted


def get_hour_data(df: pd.DataFrame, hour_label: str) -> pd.DataFrame:
    tz = datetime.now().astimezone().tzinfo
    if hour_label == "Now":
        start = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    else:
        today = datetime.now(tz).date()
        hour = int(hour_label.split(':')[0])
        start = datetime.combine(today, datetime.min.time(), tzinfo=tz).replace(hour=hour)
    end = start + timedelta(hours=1)
    mask = (df["timestamp"] >= start) & (df["timestamp"] < end)
    return df.loc[mask].reset_index(drop=True)


def build_hour_table(df: pd.DataFrame, metrics: List[str], start: datetime) -> pd.DataFrame:
    times = [start + timedelta(minutes=5 * i) for i in range(12)]
    columns = [t.strftime("%H:%M") for t in times]
    rows = []
    for metric in metrics:
        values = []
        for t in times:
            row = df.loc[df["timestamp"] == t]
            if row.empty:
                values.append(None)
            else:
                val = row.iloc[0].get(metric)
                values.append(val)
        rows.append({"Metric": human_metric(metric), **dict(zip(columns, values))})
    return pd.DataFrame(rows)


def format_signal_bars(latency_ms: Optional[float]) -> str:
    thresholds = [150, 250, 400, float('inf')]
    level = 0 if latency_ms is None else next((idx + 1 for idx, threshold in enumerate(thresholds) if latency_ms < threshold), 4)
    bars = ''.join(
        f"<span class='signal-bar {'on' if i < level else 'off'}'></span>"
        for i in range(4)
    )
    return bars


def refresh_status() -> Dict[str, object]:
    vpn = VPNConnection()
    try:
        return vpn.status()
    except Exception as exc:
        logger.exception("Failed to refresh VPN status")
        return {
            "is_connected": False,
            "vpn_ip": None,
            "pid": None,
            "error": str(exc),
        }


def run_health_probe() -> Dict[str, object]:
    cfg = load_umg_config()
    client = JanitzaUMG(
        host=cfg.get("host"),
        http_port=cfg.get("http_port"),
        modbus_port=cfg.get("modbus_port"),
        timeout_s=cfg.get("timeout_s"),
        registers=cfg.get("registers"),
    )
    return client.health()


if "selected_metrics" not in st.session_state:
    st.session_state["selected_metrics"] = METRIC_ORDER[:4]
if "health" not in st.session_state:
    st.session_state["health"] = run_health_probe()

vpn_status = refresh_status()
daily_df = read_daily_data()
latest_row = daily_df.iloc[-1] if not daily_df.empty else None
status_label = latest_row.get("status") if latest_row is not None else "unknown"

col_run, col_stop, spacer, col_health = st.columns([1, 1, 6, 1])
with col_run:
    if st.button("RUN", key="run_btn", help="Start continuous polling", disabled=BACKGROUND_POLLER.is_running(), use_container_width=True):
        try:
            BACKGROUND_POLLER.start(interval_s=60, cycles=None, align_to_minute=True)
            reset_auto_refresh()
            st.toast("Continuous polling started", icon="✅")
        except RuntimeError as exc:
            st.toast(str(exc), icon="⚠️")
with col_stop:
    if st.button("STOP", key="stop_btn", help="Stop background polling", disabled=not BACKGROUND_POLLER.is_running(), use_container_width=True):
        if BACKGROUND_POLLER.stop():
            reset_auto_refresh()
            st.toast("Polling stopped", icon="🛑")
        else:
            st.toast("Polling was not running", icon="ℹ️")
with col_health:
    if st.button("Check Health", key="health_btn", use_container_width=True):
        st.session_state["health"] = run_health_probe()
        reset_auto_refresh()

health = st.session_state["health"]

left_col, right_col = st.columns([1, 2], gap="large")

with left_col:
    st.subheader("Last Value")
    if latest_row is not None:
        left_df = build_left_table(latest_row)
        st.dataframe(
            style_left_table(left_df, status_label),
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("No readings captured yet.")

    st.markdown("### VPN Status")
    vpn_box = st.container()
    with vpn_box:
        if vpn_status.get("error"):
            st.error(f"VPN status unavailable: {vpn_status['error']}")
        st.markdown(f"**Connected:** {'✅' if vpn_status.get('is_connected') else '❌'}")
        st.markdown(f"**VPN IP:** {vpn_status.get('vpn_ip') or 'N/A'}")
        st.markdown(f"**PID:** {vpn_status.get('pid') or 'N/A'}")
        tunnel_state = "RUNNING" if vpn_status.get("is_connected") else "STOPPED"
        st.markdown(f"**Tunnel State:** {tunnel_state}")

    st.markdown("### Teltonika RUT240")
    signal_html = format_signal_bars(health.get('modbus_ms'))
    connection_state = 'ONLINE' if health.get('reachable') else 'OFFLINE'
    connection_color = 'green' if connection_state == 'ONLINE' else 'red'
    st.markdown(
        f"**Status:** <span style='color:{connection_color}; font-weight:700'>{connection_state}</span><br>"
        f"**Signal Power:** {signal_html}<br>"
        f"**Firmware:** <span style='color:green'>UPDATE</span><br>"
        f"**IP UMG:** {load_umg_config().get('host', 'N/A')}",
        unsafe_allow_html=True,
    )

with right_col:
    st.subheader("Interval View")
    hour_options = collect_hour_options(daily_df)
    selected_hour = st.selectbox("Select hour", hour_options, index=0)

    tz = datetime.now().astimezone().tzinfo
    if selected_hour == "Now":
        start_hour = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    else:
        today = datetime.now(tz).date()
        hour = int(selected_hour.split(':')[0])
        start_hour = datetime.combine(today, datetime.min.time(), tzinfo=tz).replace(hour=hour)

    st.markdown(f"**Actual Value =** {start_hour:%H:%M} | {start_hour:%d.%m.%Y}")

    metrics_selection = st.multiselect(
        "Select metrics",
        [human_metric(m) for m in METRIC_ORDER],
        [human_metric(m) for m in st.session_state["selected_metrics"]],
    )
    if metrics_selection:
        st.session_state["selected_metrics"] = [
            m for m in METRIC_ORDER if human_metric(m) in metrics_selection
        ]
    selected_metrics = st.session_state["selected_metrics"] or METRIC_ORDER[:4]

    hour_df = get_hour_data(daily_df, selected_hour)
    hour_table = build_hour_table(hour_df, selected_metrics, start_hour)
    hour_columns = [col for col in hour_table.columns if col != "Metric"]
    st.dataframe(hour_table, hide_index=True, use_container_width=True)

    graph_mode = st.radio("Select graph", ["Actual Value", "All day"], horizontal=True)
    if graph_mode == "Actual Value":
        graph_df = hour_df
    else:
        graph_df = daily_df

    if not graph_df.empty:
        plot_df = graph_df[["timestamp", *selected_metrics]].melt(
            id_vars="timestamp",
            value_vars=selected_metrics,
            var_name="Metric",
            value_name="Value",
        )
        plot_df["Metric"] = plot_df["Metric"].map(human_metric)
        fig = px.line(plot_df, x="timestamp", y="Value", color="Metric", markers=True)
        fig.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No data available for selected range.")

# Error log display
if BACKGROUND_POLLER.last_error:
    st.error(f"Last background error: {BACKGROUND_POLLER.last_error}")

if st.button("Refresh", key="refresh_btn"):
    reset_auto_refresh()
    st.experimental_rerun()
