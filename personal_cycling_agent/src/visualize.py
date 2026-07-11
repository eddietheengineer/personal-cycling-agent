"""
Streamlit visualization dashboard for cycling telemetry data.

Launch with:
    streamlit run src/visualize.py
    or
    python3 -m src.main --visualize

Requires plotly and streamlit in requirements.txt.

NOTE on units from Garmin Connect:
  - duration is stored in milliseconds
  - distance is stored in centimeters
  - speed in activity_streams is m/s
  - elapsed in activity_streams is seconds
"""

import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Ensure project root is on sys.path so `src.*` imports work regardless of how
# the script is invoked (streamlit run sets cwd to the script's directory).
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import config
from src.db.store import CyclingDB
from src.analytics.training_load import compute_training_load_history


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
# Handle Home Assistant Ingress path prefix
# Streamlit checks HASSIO_INGRESS env var for native ingress support
st.set_page_config(page_title="Cycling Agent", layout="wide")
st.title("Cycling Dashboard")


# ---------------------------------------------------------------------------
# DB connection (persisted across reruns via session_state)
# ---------------------------------------------------------------------------
if "db" not in st.session_state:
    try:
        config.setup()
        db_path = config.db_path("cycling_agent.sqlite")
        if not db_path.exists():
            st.error(f"Database not found at `{db_path}`. Run `--ingest` first.")
            st.stop()
        st.session_state.db = CyclingDB(str(db_path))
    except Exception as exc:
        st.error(f"Failed to open database: {exc}")
        st.stop()

db = st.session_state.db


# ---------------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------------
st.sidebar.header("Dashboard")

if "nav_page" not in st.session_state:
    st.session_state.nav_page = "Activity Detail"

nav_page = st.sidebar.selectbox(
    "Navigate",
    ["Activity Detail", "Trends", "Map", "Profile", "Settings"],
    index=["Activity Detail", "Trends", "Map", "Profile", "Settings"].index(
        st.session_state.nav_page
    ),
    label_visibility="collapsed",
)
if nav_page != st.session_state.nav_page:
    st.session_state.nav_page = nav_page
    st.rerun()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _downsample(elapsed: list, values: list, max_points: int = 10_000) -> tuple[list, list]:
    """Uniformly downsample to at most *max_points* points."""
    n = len(values)
    if n <= max_points:
        return elapsed, values
    step = max(1, n // max_points)
    idx = np.arange(0, n, step)
    return [elapsed[i] for i in idx], [values[i] for i in idx]


def _elapsed_to_minutes(seconds: float) -> float:
    return seconds / 60.0


def _format_duration(ms: float | None) -> str:
    """Format Garmin duration (milliseconds) into human-readable string."""
    if ms is None:
        return "—"
    sec = int(ms / 1000)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _distance_km(cm: float | None) -> str:
    """Format Garmin distance (centimeters) into km string."""
    if cm is None or cm == 0:
        return "—"
    return f"{cm / 100000:.2f} km"


def _stream_id(activity_id: str) -> str:
    """Strip 'garmin_' prefix for activity_streams lookup."""
    if activity_id.startswith("garmin_"):
        return activity_id[len("garmin_"):]
    return activity_id


# --- Zone definitions (theme-aware colors) ---

# Power zones (based on FTP): Z1 <55%, Z2 55-75%, Z3 76-90%, Z4 91-105%, Z5 >105%
_ZONE_RANGES = [
    (0.0, 0.55, "Z1: Active Recovery"),
    (0.55, 0.75, "Z2: Endurance"),
    (0.76, 0.90, "Z3: Tempo"),
    (0.91, 1.05, "Z4: Threshold"),
    (1.05, 999, "Z5: VO2/Neuromuscular"),
]

_LIGHT_COLORS = ["#1f77b4", "#2ca02c", "#ff7f0e", "#d62728", "#9467bd"]
_DARK_COLORS = ["#5cb8e0", "#6ecf6e", "#ffb347", "#ff7b7b", "#c99fff"]


def _zone_for_value(value: float, threshold: float, ranges: list) -> int:
    """Return zone index (0-4) for a value relative to a threshold."""
    if threshold <= 0:
        return -1
    ratio = value / threshold
    for idx, (lo, hi, _) in enumerate(ranges):
        if lo <= ratio <= hi:
            return idx
    return -1


def _zone_colors():
    """Return zone color list matching current Streamlit theme."""
    theme = st.query_params.get("theme", "light")
    if theme == "dark":
        return _DARK_COLORS
    return _LIGHT_COLORS


def _make_zones(ranges: list, colors: list) -> list:
    """Build zone tuples from ranges and color list."""
    return [(lo, hi, label, color) for (lo, hi, label), color in zip(ranges, colors)]


_POWER_ZONES = _make_zones(_ZONE_RANGES, _LIGHT_COLORS)

# HR zones (based on Max HR): Z1 <58%, Z2 59-74%, Z3 75-89%, Z4 90-94%, Z5 >95%
_HR_RANGES = [
    (0.0, 0.58, "Z1: Active Recovery"),
    (0.59, 0.74, "Z2: Endurance"),
    (0.75, 0.89, "Z3: Tempo"),
    (0.90, 0.94, "Z4: Threshold"),
    (0.95, 999, "Z5: VO2/Neuromuscular"),
]
_HR_ZONES = _make_zones(_HR_RANGES, _LIGHT_COLORS)


def _build_zone_chart(
    elapsed: list, values: list, threshold: float, zones: list,
    y_label: str, title: str,
) -> go.Figure:
    """Build a chart with colored zone background bands and a single data line."""
    n = len(elapsed)
    x_min = [_elapsed_to_minutes(e) for e in elapsed]
    data_max = max(values) if values else 0

    # Detect theme for line color
    theme = st.query_params.get("theme", "light")
    line_color = "#f0f0f0" if theme == "dark" else "#222222"

    fig = go.Figure()

    # Colored background bands for each zone
    for lo, hi, label, color in zones:
        if threshold > 0:
            y_lo = lo * threshold
            y_hi = min(hi * threshold, data_max * 1.05)
            fig.add_hrect(
                y0=y_lo, y1=y_hi,
                fillcolor=color, opacity=0.12,
                line=dict(width=0),
                layer="below",
            )

    # Single continuous line for the data
    if n > 0:
        fig.add_trace(go.Scatter(
            x=x_min, y=values, mode="lines",
            line=dict(width=2, color=line_color),
            hovertemplate=f"Elapsed: %{{x:.1f}} min<br>{y_label}: %{{y:.0f}}<extra></extra>",
            name="",
        ))

    fig.update_layout(
        title=title,
        template="plotly_white" if theme != "dark" else "plotly_dark",
        height=360,
        margin=dict(l=50, r=20, t=40, b=40),
        yaxis=dict(title=y_label),
        xaxis=dict(title="Elapsed (min)"),
        showlegend=False,
    )
    return fig


# ---------------------------------------------------------------------------
# Activity Detail tab
# ---------------------------------------------------------------------------
def _render_activity_detail():
    activities = db.get_activities()

    if not activities:
        st.info("No activities found. Run `--ingest` first.")
        return

    # Build display labels sorted by date descending (most recent first)
    options = []
    id_map = {}
    for a in activities:
        label = f"{a['start_date'][:10]} — {a['activity_type']}"
        options.append(label)
        id_map[label] = a["id"]

    selected_label = st.selectbox("Select activity", options)
    selected_id = id_map[selected_label]

    # Fetch combined activity + computed metrics
    combined = db.get_activity_with_metrics(selected_id)
    if combined is None:
        st.warning("Activity not found in database.")
        return

    # -- Metadata cards --
    st.subheader(f"Activity: {combined['activity_type']}")
    st.caption(f"ID: {combined['id']}")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Date", combined["start_date"][:10])
    col2.metric("Duration", _format_duration(combined.get("duration")))
    col3.metric("Distance", _distance_km(combined.get("distance")))
    col4.metric("Calories", f"{combined.get('calories', 0):.0f}" if combined.get("calories") else "—")

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Avg Power", f"{combined.get('average_power', 0):.0f} W" if combined.get("average_power") else "—")
    col6.metric("Max Power", f"{combined.get('max_power', 0):.0f} W" if combined.get("max_power") else "—")
    col7.metric("Avg HR", f"{combined.get('average_hr', 0):.0f} bpm" if combined.get("average_hr") else "—")
    col8.metric("Max HR", f"{combined.get('max_hr', 0):.0f} bpm" if combined.get("max_hr") else "—")

    # Computed metrics (if available)
    computed_fields = {
        "FTP Used": ("ftp_used", "W"),
        "Normalized Power": ("normalized_power", "W"),
        "Intensity Factor": ("intensity_factor", ""),
        "TSS": ("tss", ""),
        "Variability Index": ("variability_index", ""),
        "W' Capacity": ("w_prime_capacity", "kJ"),
        "Decoupling Drift": ("decoupling_drift", "%"),
    }

    computed_cols = st.columns(len(computed_fields))
    for idx, (label, (key, unit)) in enumerate(computed_fields.items()):
        val = combined.get(key)
        if val is not None:
            if key == "intensity_factor":
                computed_cols[idx].metric(label, f"{val:.2f}")
            elif key == "decoupling_drift":
                computed_cols[idx].metric(label, f"{val:.1f}%")
            elif unit == "kJ":
                computed_cols[idx].metric(label, f"{val:.1f} kJ")
            else:
                computed_cols[idx].metric(label, f"{val:.1f} {unit}")
        else:
            computed_cols[idx].metric(label, "—")

    # -- Stream charts --
    stream_metrics = ["power", "heart_rate", "speed", "cadence", "altitude"]
    metric_labels = {
        "power": "Power (W)",
        "heart_rate": "Heart Rate (bpm)",
        "speed": "Speed (km/h)",
        "cadence": "Cadence (rpm)",
        "altitude": "Altitude (m)",
    }

    sid = _stream_id(selected_id)

    # Determine zone thresholds from activity data
    ftp = combined.get("ftp_used") or combined.get("average_power") or 0.0
    max_hr = combined.get("max_hr") or 0.0

    has_any_stream = False
    for metric in stream_metrics:
        rows = db.get_activity_streams(sid, metric)
        if not rows:
            continue

        has_any_stream = True
        elapsed = [r["elapsed"] for r in rows]
        values = [r["value"] for r in rows]
        elapsed, values = _downsample(elapsed, values)

        # Garmin speed is in m/s; convert to km/h for display
        if metric == "speed":
            values = [v * 3.6 for v in values]

        y_label = metric_labels.get(metric, metric)
        title = y_label

        if metric == "power" and ftp > 0:
            colors = _zone_colors()
            zones = _make_zones(_ZONE_RANGES, colors)
            fig = _build_zone_chart(elapsed, values, ftp, zones, y_label, title)
            st.plotly_chart(fig, width="stretch")
        elif metric == "heart_rate" and max_hr > 0:
            colors = _zone_colors()
            zones = _make_zones(_HR_RANGES, colors)
            fig = _build_zone_chart(elapsed, values, max_hr, zones, y_label, title)
            st.plotly_chart(fig, width="stretch")
        else:
            fig = px.line(
                x=[_elapsed_to_minutes(e) for e in elapsed],
                y=values,
                labels={"x": "Elapsed (min)", "y": y_label},
                title=title,
                template="plotly_white",
            )
            fig.update_traces(line=dict(width=1.5))
            fig.update_layout(height=320, margin=dict(l=50, r=20, t=40, b=40))
            st.plotly_chart(fig, width="stretch")

    if not has_any_stream:
        st.warning("No stream data for this activity.")


# ---------------------------------------------------------------------------
# Trends tab
# ---------------------------------------------------------------------------
def _render_trends():
    # Date range selector — default to full range of wellness data
    wellness_rows = db.get_trend_data("wellness", ["date"])

    if not wellness_rows:
        st.info("No wellness data found. Run `--ingest` first.")
        return

    all_dates = [r["date"] for r in wellness_rows]
    min_date = min(all_dates)
    max_date = max(all_dates)

    default_start = date.fromisoformat(min_date)
    default_end = date.fromisoformat(max_date)

    date_range = st.date_input(
        "Date range",
        value=(default_start, default_end),
        min_value=default_start,
        max_value=default_end,
    )

    if len(date_range) != 2:
        return

    oldest, newest = date_range[0].isoformat(), date_range[1].isoformat()

    # ---- FTP over time ----
    metrics_rows = db.get_activity_metrics_by_date(oldest, newest)
    ftp_chart_data = []
    tss_by_date: dict[str, float] = {}

    for row in metrics_rows:
        sd = row.get("start_date")
        if not sd:
            continue

        if row.get("ftp_used") is not None:
            ftp_chart_data.append({"date": sd, "ftp_used": row["ftp_used"]})

        if row.get("tss") is not None:
            d = sd[:10]
            tss_by_date[d] = tss_by_date.get(d, 0.0) + row["tss"]

    if ftp_chart_data:
        df_ftp = pd.DataFrame(ftp_chart_data)
        fig = px.line(
            df_ftp, x="date", y="ftp_used",
            labels={"ftp_used": "FTP Used (W)"},
            title="FTP Over Time",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=2))
        fig.update_layout(height=300)
        st.plotly_chart(fig, width="stretch")

    # ---- TSS / CTL / ATL ----
    if tss_by_date:
        sorted_dates = sorted(tss_by_date.keys())
        tss_records = [{"date": d, "tss": tss_by_date[d]} for d in sorted_dates]

        history = compute_training_load_history(tss_records)

        if history:
            df_load = pd.DataFrame(history)
            fig = px.line(
                df_load, x="date", y=["ctl", "atl"],
                labels={"value": "Training Load", "variable": ""},
                title="CTL / ATL Over Time",
                template="plotly_white",
                color_discrete_map={"ctl": "#1f77b4", "atl": "#ff7f0e"},
            )
            fig.update_traces(line=dict(width=2))
            fig.update_layout(height=300, legend=dict(title=""))
            st.plotly_chart(fig, width="stretch")

            # TSB
            fig_tsb = px.line(
                df_load, x="date", y="tsb",
                labels={"tsb": "TSB", "date": ""},
                title="Training Stress Balance (CTL − ATL)",
                template="plotly_white",
            )
            fig_tsb.update_traces(line=dict(width=2, color="#2ca02c"))
            fig_tsb.update_layout(height=250)
            st.plotly_chart(fig_tsb, width="stretch")

    # ---- Wellness trends ----
    wellness_data = db.get_trend_data(
        "wellness",
        ["date", "rmssd", "resting_hr", "weight", "stress", "sleep_hours", "sleep_score"],
        oldest,
        newest,
    )

    if not wellness_data:
        return

    df_wellness = pd.DataFrame(wellness_data)

    # HRV (RMSSD)
    rmssd_rows = df_wellness[df_wellness["rmssd"].notna()]
    if not rmssd_rows.empty:
        fig = px.line(
            rmssd_rows, x="date", y="rmssd",
            labels={"rmssd": "RMSSD (ms)"},
            title="HRV (RMSSD)",
            template="plotly_white",
        )
        mean_rmssd = rmssd_rows["rmssd"].mean()
        fig.add_hline(y=mean_rmssd, line_dash="dash", annotation_text=f"Mean: {mean_rmssd:.0f}")
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")

    # Resting HR
    rhr_rows = df_wellness[df_wellness["resting_hr"].notna()]
    if not rhr_rows.empty:
        fig = px.line(
            rhr_rows, x="date", y="resting_hr",
            labels={"resting_hr": "Resting HR (bpm)"},
            title="Resting Heart Rate",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")

    # Weight
    weight_rows = df_wellness[df_wellness["weight"].notna()]
    if not weight_rows.empty:
        fig = px.line(
            weight_rows, x="date", y="weight",
            labels={"weight": "Weight (kg)"},
            title="Weight",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")

    # Sleep
    sleep_rows = df_wellness[
        df_wellness["sleep_hours"].notna() | df_wellness["sleep_score"].notna()
    ]
    if not sleep_rows.empty:
        fig = go.Figure()
        if sleep_rows["sleep_hours"].notna().any():
            fig.add_trace(go.Scatter(
                x=sleep_rows["date"], y=sleep_rows["sleep_hours"],
                name="Hours", mode="lines",
                line=dict(width=2, color="#9467bd"),
            ))
        if sleep_rows["sleep_score"].notna().any():
            fig.add_trace(go.Scatter(
                x=sleep_rows["date"], y=sleep_rows["sleep_score"],
                name="Score", mode="lines",
                line=dict(width=2, color="#1f77b4"),
            ))
        fig.update_layout(
            title="Sleep",
            height=280,
            template="plotly_white",
            yaxis=dict(title="Hours / Score"),
            legend=dict(title=""),
        )
        st.plotly_chart(fig, width="stretch")

    # Stress
    stress_rows = df_wellness[df_wellness["stress"].notna()]
    if not stress_rows.empty:
        fig = px.line(
            stress_rows, x="date", y="stress",
            labels={"stress": "Stress"},
            title="Daily Stress",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")



# ---------------------------------------------------------------------------
# Map tab
# ---------------------------------------------------------------------------
def _geocode_city(city: str) -> tuple[float, float] | None:
    """Geocode a city name to (lat, lon) using Nominatim. Cached in session_state."""
    if city not in st.session_state:
        from geopy.geocoders import Nominatim
        from geopy.exc import GeopyError
        try:
            geolocator = Nominatim(user_agent="personal-cycling-agent")
            location = geolocator.geocode(city)
            if location is not None:
                st.session_state[city] = (location.latitude, location.longitude)
        except GeopyError:
            st.session_state[city] = None
    return st.session_state.get(city)


def _render_map():
    """Render a map tab with heatmap of route activity."""
    from geopy.distance import geodesic

    st.subheader("Route Map")

    # Sidebar inputs for geographic filter
    city = st.text_input("City", value="Louisville, Kentucky")
    radius_miles = st.slider("Radius (miles)", min_value=10, max_value=500, value=100, step=10)

    # Geocode the city
    center = _geocode_city(city)
    if center is None:
        st.error(f"Could not geocode \"{city}\". Try a more specific address.")
        return

    center_lat, center_lon = center
    st.info(f"Center: {city} ({center_lat:.4f}, {center_lon:.4f}) — Radius: {radius_miles} mi")

    # Get distinct activity IDs with route data
    route_ids = db.conn.execute(
        "SELECT DISTINCT activity_id FROM activity_routes"
    ).fetchall()

    if not route_ids:
        st.info("No route data found. Run --sync-routes first.")
        return

    # For each activity, compute centroid and check if within radius
    matching_ids: list[str] = []
    all_points: list[dict] = []

    for (activity_id,) in route_ids:
        # Get centroid of this activity's route
        centroid = db.conn.execute(
            "SELECT AVG(latitude), AVG(longitude) FROM activity_routes WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()

        if centroid[0] is None or centroid[1] is None:
            continue

        dist = geodesic((center_lat, center_lon), (centroid[0], centroid[1])).miles

        if dist <= radius_miles:
            matching_ids.append(activity_id)
            # Fetch all points for this activity
            points = db.conn.execute(
                "SELECT latitude, longitude FROM activity_routes WHERE activity_id = ? ORDER BY sequence",
                (activity_id,),
            ).fetchall()
            for (lat, lon) in points:
                all_points.append({"lat": lat, "lon": lon, "activity_id": activity_id})

    if not all_points:
        st.info(f"No routes found within {radius_miles} mi of {city}.")
        return

    df = pd.DataFrame(all_points)

    # Stats
    col1, col2, col3 = st.columns(3)
    col1.metric("Activities", len(matching_ids))
    col2.metric("Total Points", len(df))
    col3.metric("Coverage", f"{radius_miles} mi radius")

    # Render map — use scatter_mapbox if token available, fallback to scatter_geo
    mapbox_token = os.environ.get("MAPBOX_TOKEN", "")

    if mapbox_token:
        fig = px.scatter_mapbox(
            df,
            lat="lat",
            lon="lon",
            color="lat",
            color_continuous_scale="Hot",
            title=f"Route Heatmap — {city} ({len(matching_ids)} activities)",
            hover_data={"activity_id": True},
            opacity=0.6,
        )
        fig.update_layout(
            mapbox={"accesstoken": mapbox_token, "style": "open-street-map", "center": {"lat": center_lat, "lon": center_lon}, "zoom": 8},
            margin={"r": 0, "t": 40, "l": 0, "b": 0},
            height=600,
        )
    else:
        fig = px.scatter_geo(
            df,
            lat="lat",
            lon="lon",
            color="lat",
            color_continuous_scale="Hot",
            title=f"Route Heatmap — {city} ({len(matching_ids)} activities) [no Mapbox token]",
            hover_data={"activity_id": True},
            opacity=0.6,
            scope="usa",
        )
        fig.update_layout(
            margin={"r": 0, "t": 40, "l": 0, "b": 0},
            height=600,
        )

    st.plotly_chart(fig, use_container_width=True)


def _render_profile():
    """Render the athlete profile editing tab."""
    profile_path = config.user_profile_path()

    # Read existing profile into a dict
    profile = {
        "name": os.getenv("ATHLETE_NAME", ""),
        "weight_kg": int(os.getenv("WEIGHT_KG", "0")),
        "height_cm": int(os.getenv("HEIGHT_CM", "0")),
        "discipline": os.getenv("DISCIPLINE", "road"),
        "ftp_watts": int(os.getenv("FTP_WATTS", "0")),
        "max_hr": int(os.getenv("MAX_HR", "0")),
        "resting_hr": int(os.getenv("RESTING_HR", "0")),
        "lt1_power": int(os.getenv("LT1_POWER", "0")),
        "lt2_power": int(os.getenv("LT2_POWER", "0")),
        "primary_goal": os.getenv("PRIMARY_GOAL", ""),
        "secondary_goal": os.getenv("SECONDARY_GOAL", ""),
        "training_days": os.getenv("TRAINING_DAYS", ""),
        "max_session_duration": os.getenv("MAX_SESSION_DURATION", ""),
        "terrain": os.getenv("TERRAIN", ""),
        "bikes": os.getenv("BIKES", ""),
        "power_meter": os.getenv("POWER_METER", ""),
        "hr_monitor": os.getenv("HR_MONITOR", ""),
    }

    if profile_path.exists():
        raw = profile_path.read_text()
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("- ") and ": " in line:
                key, val = line[2:].split(": ", 1)
                key = key.lower().replace(" ", "_").replace("(watts)", "").replace("(if_known)", "").replace("(avg)", "").replace("(kg)", "").replace("(cm)", "")
                # Normalize known keys
                key_map = {
                    "name": "name",
                    "weight_kg": "weight_kg",
                    "height_cm": "height_cm",
                    "primary_discipline": "discipline",
                    "ftp_watts": "ftp_watts",
                    "max_hr": "max_hr",
                    "resting_hr_avg": "resting_hr",
                    "lt1_power_if_known": "lt1_power",
                    "lt2_power_if_known": "lt2_power",
                    "primary_goal": "primary_goal",
                    "secondary_goal": "secondary_goal",
                    "available_training_days": "training_days",
                    "max_session_duration": "max_session_duration",
                    "terrain_notes": "terrain",
                    "bike(s)": "bikes",
                    "power_meter": "power_meter",
                    "hr_monitor": "hr_monitor",
                }
                k = key_map.get(key, key)
                if k in profile:
                    if isinstance(profile[k], int):
                        try:
                            profile[k] = int(val.rstrip("W").strip())
                        except ValueError:
                            pass
                    else:
                        profile[k] = val

    st.subheader("Athlete Profile")
    st.caption("Edit your profile below. Changes are saved to your vault directory and used by the analytics engine.")

    with st.form("profile_form", clear_on_submit=False):
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Identity")
            name = st.text_input("Name", value=profile["name"], key="prof_name")
            weight = st.number_input("Weight (kg)", min_value=0, value=profile["weight_kg"], key="prof_weight")
            height = st.number_input("Height (cm)", min_value=0, value=profile["height_cm"], key="prof_height")

            st.subheader("Training")
            disciplines = ["road", "gravel", "MTB", "TT"]
            discipline = st.selectbox("Primary Discipline", disciplines,
                                      index=disciplines.index(profile["discipline"]) if profile["discipline"] in disciplines else 0,
                                      key="prof_discipline")

        with col2:
            st.subheader("Physiological Baselines")
            ftp = st.number_input("FTP (watts)", min_value=0, value=profile["ftp_watts"], key="prof_ftp")
            max_hr = st.number_input("Max HR", min_value=0, value=profile["max_hr"], key="prof_max_hr")
            resting_hr = st.number_input("Resting HR (avg)", min_value=0, value=profile["resting_hr"], key="prof_resting_hr")
            lt1 = st.number_input("LT1 Power (watts)", min_value=0, value=profile["lt1_power"], key="prof_lt1")
            lt2 = st.number_input("LT2 Power (watts)", min_value=0, value=profile["lt2_power"], key="prof_lt2")

        st.subheader("Goals & Constraints")
        goal_col1, goal_col2 = st.columns(2)
        with goal_col1:
            primary_goal = st.text_input("Primary Goal", value=profile["primary_goal"], key="prof_primary_goal")
            training_days = st.text_input("Available Training Days", value=profile["training_days"],
                                          placeholder="e.g., Mon,Wed,Fri", key="prof_training_days")
        with goal_col2:
            secondary_goal = st.text_input("Secondary Goal", value=profile["secondary_goal"], key="prof_secondary_goal")
            max_session = st.text_input("Max Session Duration", value=profile["max_session_duration"],
                                        placeholder="e.g., 2h", key="prof_max_session")

        terrain = st.text_area("Terrain Notes", value=profile["terrain"],
                                placeholder="e.g., flat, hilly, mountainous", key="prof_terrain")

        st.subheader("Equipment")
        equip_col1, equip_col2 = st.columns(2)
        with equip_col1:
            bikes = st.text_input("Bike(s)", value=profile["bikes"], key="prof_bikes")
        with equip_col2:
            power_meter = st.text_input("Power Meter", value=profile["power_meter"], key="prof_power_meter")
            hr_monitor = st.text_input("HR Monitor", value=profile["hr_monitor"], key="prof_hr_monitor")

        submitted = st.form_submit_button("Save Profile")

        if submitted:
            content = f"""# Athlete Profile

## Identity
- Name: {name}
- Weight (kg): {int(weight)}
- Height (cm): {int(height)}

## Training History
- Primary discipline: {discipline}

## Physiological Baselines
- FTP (watts): {int(ftp)}
- Max HR: {int(max_hr)}
- Resting HR (avg): {int(resting_hr)}
- LT1 power (if known): {int(lt1)}
- LT2 power (if known): {int(lt2)}

## Goals & Constraints
- Primary goal: {primary_goal}
- Secondary goal: {secondary_goal}
- Available training days: {training_days}
- Max session duration: {max_session}
- Terrain notes: {terrain}

## Equipment
- Bike(s): {bikes}
- Power meter: {power_meter}
- HR monitor: {hr_monitor}
"""
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(content)
            st.success("Profile saved!")


# ---------------------------------------------------------------------------
# Garmin Setup tab
# ---------------------------------------------------------------------------
def _update_config_env(updates: dict) -> None:
    """Update KEY=VALUE pairs in config.env and reload into os.environ."""
    env_path = config.config_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        lines = []
    existing_keys = {l.split("=", 1)[0] for l in lines if "=" in l}
    for k, v in updates.items():
        if k == "GARMIN_PASSWORD":
            os.environ[k] = v
            hashed, _ = config.hash_password(v)
            v = hashed
        if k in existing_keys:
            lines = [f"{k}={v}" if l.startswith(f"{k}=") else l for l in lines]
        else:
            lines.append(f"{k}={v}")
    env_path.write_text("\n".join(lines) + "\n")
    for k, v in updates.items():
        if k != "GARMIN_PASSWORD":
            os.environ[k] = v


def _render_garmin_setup():
    """Render the Garmin Connect setup and sync tab."""
    from src.ingestion.garmin_connect import sync_garmin, sync_activities
    from src.ingestion.garmin_export import sync_routes_from_fit

    st.subheader("Garmin Connect Setup")
    st.caption("Configure your Garmin Connect credentials and sync activities.")

    # Read current email from env
    current_email = os.environ.get("GARMIN_EMAIL", "")
    has_credentials = bool(current_email and os.environ.get("GARMIN_PASSWORD", ""))

    # Show current status
    activities = db.get_activities()
    wellness_rows = db.get_trend_data("wellness", ["date"])

    col1, col2, col3 = st.columns(3)
    col1.metric("Activities", len(activities))
    col2.metric("Wellness Days", len(wellness_rows))
    col3.metric("Status", "Connected" if has_credentials else "Not configured")

    if has_credentials:
        st.success(f"Connected as: {current_email}")

    # ── Credentials form ─────────────────────────────────────────────
    with st.form("garmin_credentials", clear_on_submit=False):
        st.subheader("Credentials")

        email = st.text_input("Garmin Email", value=current_email, key="garmin_email_input")
        password = st.text_input("Garmin Password", type="password", key="garmin_password_input")

        save_clicked = st.form_submit_button("Save Credentials", type="primary")

        if save_clicked:
            if not email:
                st.error("Email is required.")
            elif not password:
                st.error("Password is required.")
            else:
                _update_config_env({
                    "GARMIN_EMAIL": email,
                    "GARMIN_PASSWORD": password,
                })
                st.success("Credentials saved! You can now sync activities.")
                st.rerun()

    # ── Sync controls ────────────────────────────────────────────────
    st.subheader("Sync")

    days = st.number_input(
        "Days to Sync",
        min_value=1,
        max_value=365,
        value=7,
        step=1,
        key="sync_days",
        help="Number of days of activity data to fetch from Garmin Connect.",
    )

    col1, col2 = st.columns(2)
    with col1:
        sync_clicked = st.button(
            "Sync Activities",
            type="primary",
            disabled=not has_credentials,
            help="Fetch wellness data and activities from Garmin Connect.",
        )
    with col2:
        routes_clicked = st.button(
            "Sync Routes",
            disabled=not has_credentials,
            help="Parse FIT files and extract route data.",
        )

    # ── Handle sync ──────────────────────────────────────────────────
    if sync_clicked:
        st.session_state.syncing = True
        st.session_state.sync_status = "Starting sync..."
        st.session_state.sync_result = None

        try:
            st.session_state.sync_status = "Fetching wellness data..."
            wellness_counts = sync_garmin(db_path=str(config.db_path("cycling_agent.sqlite")))

            st.session_state.sync_status = "Fetching activity streams..."
            activity_counts = sync_activities(days=days, db_path=str(config.db_path("cycling_agent.sqlite")))

            st.session_state.sync_result = {
                "wellness": wellness_counts,
                "activities": activity_counts,
            }
            st.session_state.sync_status = "Sync complete!"
            st.session_state.syncing = False
            st.success("Sync complete!")
            st.rerun()
        except Exception as exc:
            st.session_state.syncing = False
            st.session_state.sync_status = f"Sync failed: {exc}"
            st.error(f"Sync failed: {exc}")

    if routes_clicked:
        try:
            raw_dir = config.raw_dir() / "fit"
            counts = sync_routes_from_fit(db, raw_dir)
            st.success(f"Route sync complete: {counts}")
            st.rerun()
        except Exception as exc:
            st.error(f"Route sync failed: {exc}")

    # ── Show sync status ─────────────────────────────────────────────
    if "sync_status" in st.session_state:
        st.info(st.session_state.sync_status)

    if st.session_state.get("syncing"):
        st.warning("Sync in progress... this may take a few minutes.")

    # ── Show sync results ────────────────────────────────────────────
    if st.session_state.get("sync_result"):
        result = st.session_state.sync_result
        st.subheader("Sync Results")
        if "wellness" in result:
            st.write(f"**Wellness:** {result['wellness']}")
        if "activities" in result:
            st.write(f"**Activities:** {result['activities']}")
# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------
def _render_settings():
    """Render the Settings page with Garmin Connect authentication."""
    from src.ingestion.garmin_connect import authenticate_garmin

    st.subheader("Garmin Connect")
    st.caption("Connect to Garmin Connect to sync your activity and wellness data.")

    # Read current email from env to show status
    current_email = os.environ.get("GARMIN_EMAIL", "")
    has_credentials = bool(current_email and os.environ.get("GARMIN_PASSWORD", ""))

    # Show current connection status
    if has_credentials:
        st.success(f"Configured as: **{current_email}**")
    else:
        st.info("No Garmin credentials configured yet.")

    # ── Deferred message display ──────────────────────────────────────
    if st.session_state.get("garmin_auth_message"):
        msg = st.session_state.garmin_auth_message
        msg_type = st.session_state.garmin_auth_message_type
        if msg_type == "success":
            st.success(msg)
        elif msg_type == "error":
            st.error(msg)
        st.session_state.garmin_auth_message = ""
        st.session_state.garmin_auth_message_type = ""

    # ── Auth state machine via session_state ──────────────────────────
    # States: "idle" | "entering" | "mfa_required" | "authenticating" | "authenticating_mfa" | "done"
    if "garmin_auth_state" not in st.session_state:
        st.session_state.garmin_auth_state = "idle"
    if "garmin_auth_email" not in st.session_state:
        st.session_state.garmin_auth_email = ""
    if "garmin_auth_password" not in st.session_state:
        st.session_state.garmin_auth_password = ""
    if "garmin_auth_error" not in st.session_state:
        st.session_state.garmin_auth_error = ""
    if "garmin_auth_instance" not in st.session_state:
        st.session_state.garmin_auth_instance = None

    auth_state = st.session_state.garmin_auth_state

    # ── Login form ────────────────────────────────────────────────────
    with st.form("garmin_login", clear_on_submit=False):
        st.subheader("Sign In")

        email = st.text_input(
            "Email",
            value=current_email if not has_credentials else "",
            placeholder="you@garmin.com",
            key="ga_email",
        )
        password = st.text_input(
            "Password",
            type="password",
            placeholder="Your Garmin Connect password",
            key="ga_password",
        )

        login_clicked = st.form_submit_button(
            "Sign In",
            type="primary",
            disabled=auth_state in ("authenticating", "authenticating_mfa"),
        )

        if login_clicked:
            if not email or not password:
                st.error("Email and password are required.")
            else:
                st.session_state.garmin_auth_email = email
                st.session_state.garmin_auth_password = password
                st.session_state.garmin_auth_state = "authenticating"
                st.session_state.garmin_auth_error = ""
                st.session_state.garmin_auth_instance = None
                st.rerun()

    # ── Authenticating state: attempt login ───────────────────────────
    if auth_state == "authenticating":
        email = st.session_state.garmin_auth_email
        password = st.session_state.garmin_auth_password

        with st.spinner("Connecting to Garmin Connect..."):
            tokenstore = os.environ.get("GARMIN_TOKENSTORE", "")
            result, auth_instance = authenticate_garmin(email, password, tokenstore)

        if result.success:
            _update_config_env({
                "GARMIN_EMAIL": email,
                "GARMIN_PASSWORD": password,
            })
            st.session_state.garmin_auth_state = "idle"
            st.session_state.garmin_auth_email = ""
            st.session_state.garmin_auth_password = ""
            st.session_state.garmin_auth_instance = None
            st.session_state.garmin_auth_message = "Connected successfully! Your Garmin account is now linked."
            st.session_state.garmin_auth_message_type = "success"
            st.rerun()
        elif result.mfa_required:
            st.session_state.garmin_auth_instance = auth_instance
            st.session_state.garmin_auth_state = "mfa_required"
            st.rerun()
        else:
            st.session_state.garmin_auth_state = "idle"
            st.session_state.garmin_auth_password = ""
            st.session_state.garmin_auth_instance = None
            st.session_state.garmin_auth_message = f"Login failed: {result.error}"
            st.session_state.garmin_auth_message_type = "error"
            st.rerun()

    # ── MFA required: ask for OTP ─────────────────────────────────────
    if auth_state == "mfa_required":
        st.warning("Two-factor authentication required.")
        st.caption("Enter the verification code from your authenticator app or SMS.")

        with st.form("garmin_mfa", clear_on_submit=False):
            mfa_code = st.text_input(
                "Verification Code",
                placeholder="Enter 6-digit code",
            )

            mfa_clicked = st.form_submit_button(
                "Verify",
                type="primary",
            )

            if mfa_clicked:
                if not mfa_code:
                    st.error("Verification code is required.")
                else:
                    st.session_state.garmin_auth_state = "authenticating_mfa"
                    st.session_state.garmin_auth_mfa_code = mfa_code
                    st.rerun()

    # ── Authenticating with MFA ───────────────────────────────────────
    if auth_state == "authenticating_mfa":
        email = st.session_state.garmin_auth_email
        password = st.session_state.garmin_auth_password
        mfa_code = st.session_state.garmin_auth_mfa_code
        auth_instance = st.session_state.garmin_auth_instance

        with st.spinner("Verifying code..."):
            tokenstore = os.environ.get("GARMIN_TOKENSTORE", "")
            result, auth_instance = authenticate_garmin(
                email, password, tokenstore,
                mfa_code=mfa_code,
                auth_instance=auth_instance,
            )

        if result.success:
            _update_config_env({
                "GARMIN_EMAIL": email,
                "GARMIN_PASSWORD": password,
            })
            st.session_state.garmin_auth_state = "idle"
            st.session_state.garmin_auth_email = ""
            st.session_state.garmin_auth_password = ""
            st.session_state.garmin_auth_instance = None
            st.session_state.garmin_auth_message = "Connected successfully! Your Garmin account is now linked."
            st.session_state.garmin_auth_message_type = "success"
            st.rerun()
        else:
            error_msg = result.error or "Invalid verification code. Please try again."
            st.session_state.garmin_auth_state = "mfa_required"
            st.session_state.garmin_auth_message = error_msg
            st.session_state.garmin_auth_message_type = "error"
            st.rerun()

    # ── Sync controls (only if connected) ─────────────────────────────
    if has_credentials:
        st.subheader("Sync Data")

        days = st.number_input(
            "Days to Sync",
            min_value=1,
            max_value=365,
            value=7,
            step=1,
            key="sync_days",
            help="Number of days of activity data to fetch.",
        )

        col1, col2 = st.columns(2)
        with col1:
            sync_clicked = st.button(
                "Sync Activities",
                type="primary",
                help="Fetch wellness data and activities from Garmin Connect.",
            )
        with col2:
            routes_clicked = st.button(
                "Sync Routes",
                help="Parse FIT files and extract route data.",
            )

        if sync_clicked:
            st.session_state.syncing = True
            st.session_state.sync_status = "Starting sync..."
            st.rerun()

        if st.session_state.get("syncing"):
            try:
                from src.ingestion.garmin_connect import sync_garmin, sync_activities

                st.session_state.sync_status = "Fetching wellness data..."
                wellness_counts = sync_garmin(
                    db_path=str(config.db_path("cycling_agent.sqlite"))
                )

                st.session_state.sync_status = "Fetching activity streams..."
                activity_counts = sync_activities(
                    days=days,
                    db_path=str(config.db_path("cycling_agent.sqlite")),
                )

                st.session_state.sync_result = {
                    "wellness": wellness_counts,
                    "activities": activity_counts,
                }
                st.session_state.syncing = False
                st.success("Sync complete!")
            except Exception as exc:
                st.session_state.syncing = False
                st.error(f"Sync failed: {exc}")
            finally:
                st.rerun()

        if routes_clicked:
            try:
                from src.ingestion.garmin_export import sync_routes_from_fit
                raw_dir = config.raw_dir() / "fit"
                counts = sync_routes_from_fit(db, raw_dir)
                st.success(f"Route sync complete: {counts}")
                st.rerun()
            except Exception as exc:
                st.error(f"Route sync failed: {exc}")

        if st.session_state.get("sync_result"):
            result = st.session_state.sync_result
            st.subheader("Sync Results")
            if "wellness" in result:
                st.write(f"**Wellness:** {result['wellness']}")
            if "activities" in result:
                st.write(f"**Activities:** {result['activities']}")

    # ── Disconnect ────────────────────────────────────────────────────
    if has_credentials:
        st.subheader("Account")
        if st.button("Disconnect Account", type="secondary"):
            _update_config_env({
                "GARMIN_EMAIL": "",
                "GARMIN_PASSWORD": "",
            })
            os.environ.pop("GARMIN_EMAIL", None)
            os.environ.pop("GARMIN_PASSWORD", None)
            st.session_state.garmin_auth_state = "idle"
            st.success("Disconnected.")
            st.rerun()


# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------
if nav_page == "Activity Detail":
    _render_activity_detail()
elif nav_page == "Trends":
    _render_trends()
elif nav_page == "Map":
    _render_map()
elif nav_page == "Profile":
    _render_profile()
elif nav_page == "Settings":
    _render_settings()