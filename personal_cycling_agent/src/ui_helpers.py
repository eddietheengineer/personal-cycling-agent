from src.config.constants import DOWNSAMPLE_MAX_POINTS, KM_TO_MILES, M_TO_FEET

"""Pure UI helper functions and constants extracted from visualize.py.

These functions have no dependency on Streamlit's ``st`` module or any
runtime state, making them safe to unit-test in isolation.
"""

import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: Optional[float]) -> str:
    """Format duration (seconds) into human-readable string."""
    if seconds is None or seconds < 0:
        return "\u2014"
    sec = int(seconds)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _distance_km(m: Optional[float]) -> str:
    """Format distance (meters) into km string."""
    if m is None or m <= 0:
        return "\u2014"
    return f"{m / 1000:.2f} km"


def _format_distance(m: Optional[float], units: str = "metric") -> str:
    """Format distance (meters) into human-readable string with unit suffix.
    
    units: "metric" for km, "imperial" for miles.
    """
    if m is None or m <= 0:
        return "\u2014"
    if units == "imperial":
        return f"{m / 1000 * KM_TO_MILES:.2f} mi"
    return f"{m / 1000:.2f} km"


def _format_elevation(m: Optional[float], units: str = "metric") -> str:
    """Format elevation (meters) into human-readable string with unit suffix."""
    if m is None:
        return "\u2014"
    if units == "imperial":
        return f"{m * M_TO_FEET:.0f} ft"
    return f"{m:.0f} m"


def _format_speed_label(units: str = "metric") -> str:
    """Return speed axis label for the given unit system."""
    return "Speed (mph)" if units == "imperial" else "Speed (km/h)"


def _format_altitude_label(units: str = "metric") -> str:
    """Return altitude axis label for the given unit system."""
    return "Altitude (ft)" if units == "imperial" else "Altitude (m)"


def _get_units_system() -> str:
    """Read the user's unit system preference from the profile.
    
    Returns "imperial" or "metric" (default).
    """
    from src.config import user_profile_path
    profile_path = user_profile_path()
    if profile_path.exists():
        try:
            text = profile_path.read_text()
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("- Units:"):
                    val = line.split(":", 1)[1].strip().lower()
                    if val in ("imperial", "metric"):
                        return val
        except Exception:
            pass
    return "metric"


def _stream_id(activity_id: str) -> str:
    """Strip 'garmin_' prefix for activity_streams lookup."""
    if activity_id.startswith("garmin_"):
        return activity_id[len("garmin_"):]
    return activity_id


# ---------------------------------------------------------------------------
# Downsampling / time helpers
# ---------------------------------------------------------------------------

def _downsample(elapsed: list, values: list, max_points: int = DOWNSAMPLE_MAX_POINTS) -> tuple[list, list]:
    """Uniformly downsample to at most *max_points* points."""
    if max_points <= 0:
        return elapsed, values
    if len(elapsed) != len(values):
        raise ValueError(
            f"elapsed and values must have the same length "
            f"(got {len(elapsed)} and {len(values)})"
        )
    import numpy as np  # noqa: F401 — needed for np.arange

    n = len(values)
    if n <= max_points:
        return elapsed, values
    step = max(1, n // max_points)
    idx = np.arange(0, n, step)[:max_points]
    return [elapsed[i] for i in idx], [values[i] for i in idx]


def _elapsed_to_minutes(seconds: float) -> float:
    """Convert seconds to minutes."""
    return seconds / 60.0


# ---------------------------------------------------------------------------
# Zone definitions
# ---------------------------------------------------------------------------

# Power zones (based on FTP): Z1 <55%, Z2 55-75%, Z3 76-90%, Z4 91-105%, Z5 >105%
_ZONE_RANGES = [
    (0.0, 0.55, "Z1: Active Recovery"),
    (0.55, 0.75, "Z2: Endurance"),
    (0.76, 0.90, "Z3: Tempo"),
    (0.91, 1.05, "Z4: Threshold"),
    (1.05, 999, "Z5: VO2/Neuromuscular"),
]

# HR zones (based on Max HR): Z1 <58%, Z2 59-74%, Z3 75-89%, Z4 90-94%, Z5 >95%
_HR_RANGES = [
    (0.0, 0.58, "Z1: Active Recovery"),
    (0.59, 0.74, "Z2: Endurance"),
    (0.75, 0.89, "Z3: Tempo"),
    (0.90, 0.94, "Z4: Threshold"),
    (0.95, 999, "Z5: VO2/Neuromuscular"),
]

_LIGHT_COLORS = ["#1f77b4", "#2ca02c", "#ff7f0e", "#d62728", "#9467bd"]
_DARK_COLORS = ["#4fc3f7", "#66dd77", "#ffab40", "#ff5252", "#c99fff"]


def _zone_for_value(value: float, threshold: float, ranges: list) -> int:
    """Return zone index (0-4) for a value relative to a threshold."""
    if threshold <= 0:
        return -1
    ratio = value / threshold
    for idx, (lo, hi, _) in enumerate(ranges):
        if lo <= ratio <= hi:
            return idx
    return -1


def _make_zones(ranges: list, colors: list) -> list:
    """Build zone tuples from ranges and color list."""
    return [(lo, hi, label, color) for (lo, hi, label), color in zip(ranges, colors)]


# ---------------------------------------------------------------------------
# Zone chart builder (takes ``st`` as a parameter to avoid module-level dep)
# ---------------------------------------------------------------------------

def _build_zone_chart(
    elapsed: list, values: list, threshold: float, zones: list,
    y_label: str, title: str, st,
) -> "go.Figure":
    """Build a chart with colored zone background bands and a single data line.

    Parameters
    ----------
    st :
        The ``streamlit`` module (or a mock thereof), passed in to avoid a
        hard module-level dependency on Streamlit.
    """
    import plotly.graph_objects as go

    n = len(elapsed)
    x_min = [_elapsed_to_minutes(e) for e in elapsed]
    data_max = max(values) if values else 0

    theme = st.get_option("theme.base")
    line_color = "#ffffff" if theme == "dark" else "#1a1a1a"

    fig = go.Figure()

    # Colored background bands for each zone
    for lo, hi, label, color in zones:
        if threshold > 0:
            y_lo = lo * threshold
            y_hi = min(hi * threshold, data_max * 1.05)
            fig.add_hrect(
                y0=y_lo, y1=y_hi,
                fillcolor=color, opacity=0.25,
                line=dict(width=0),
                layer="below",
            )

    # Single continuous line for the data
    if n > 0:
        fig.add_trace(go.Scatter(
            x=x_min, y=values, mode="lines",
            line=dict(width=2.5, color=line_color),
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
# Profile parsing
# ---------------------------------------------------------------------------

_PROFILE_KEY_MAP = {
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
    "gender": "gender",
    "power_meter": "power_meter",
    "hr_monitor": "hr_monitor",
    "units": "units",
}

_PROFILE_KEY_NORMALIZATIONS = [
    ("(watts)", "_watts"),
    ("(if known)", "_if_known"),
    ("(avg)", "_avg"),
    ("(kg)", "_kg"),
    ("(cm)", "_cm"),
]


def _parse_profile_text(raw: str | None, profile: dict) -> dict:
    """Parse a profile text file and update *profile* in place.

    Expects lines of the form ``- Key: value`` (Markdown bullet style).
    Returns the updated *profile* dict for convenience.
    """
    if not raw:
        return profile
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("- ") and ": " in line:
            key, val = line[2:].split(": ", 1)
            # Normalize: lower-case, strip parenthetical suffixes, replace spaces
            key = key.lower()
            for old, new in _PROFILE_KEY_NORMALIZATIONS:
                key = key.replace(" " + old, new)
            key = key.replace(" ", "_").rstrip("_")
            # Map to profile dict keys
            k = _PROFILE_KEY_MAP.get(key, key)
            if k in profile:
                if isinstance(profile[k], int):
                    v = val.strip()
                    if v.startswith("["):
                        pass  # placeholder like "[Insert ...]" \u2014 leave at 0
                    else:
                        m = re.search(r"(\d+)", v)
                        if m:
                            try:
                                profile[k] = int(m.group(1))
                            except (ValueError, OverflowError):
                                pass  # leave at default if conversion fails
                else:
                    profile[k] = val
    return profile