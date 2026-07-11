"""
Cycling Power Metrics.

Computes normalized power, intensity factor, TSS, variability index,
time-in-zones, and power-duration curves from per-second power samples.
Also estimates Critical Power from a history of activities.

Conventions:
- Power samples are 1-second intervals (dt=1).
- Coggan 5-zone model for time-in-zones.
- 4th-power NP with 30-second moving average (Intervals.icu method).
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Coggan 5-zone FTP boundaries (fractional)
_ZONE_BOUNDARIES = [0.0, 0.56, 0.75, 0.90, 1.05, float("inf")]
_ZONE_NAMES = ["Z1", "Z2", "Z3", "Z4", "Z5"]

# Power-duration curve durations in seconds
_PDC_DURATIONS = [1, 3, 5, 10, 30, 60, 120, 180, 300, 600, 1200, 1800, 3600]


@dataclass
class PowerMetricsResult:
    """Power analysis for a single activity."""

    activity_id: str
    normalized_power: float  # NP in watts
    intensity_factor: float  # IF = NP / FTP
    tss: float  # Training Stress Score
    variability_index: float  # VI = avg_power / NP
    time_in_zones: dict[str, float]  # zone name -> percentage (0-100)
    power_duration_curve: dict[int, float]  # duration_sec -> best watts


def _moving_average(arr: np.ndarray, window: int) -> np.ndarray:
    """Compute a trailing moving average; edges use whatever is available."""
    n = len(arr)
    if window <= 1:
        return arr.copy()
    # cumsum-based moving average for speed
    cumsum = np.cumsum(arr)
    cumsum = np.insert(cumsum, 0, 0.0)
    # For each position i, average over [max(0, i - window + 1), i + 1)
    result = np.empty(n, dtype=np.float64)
    for i in range(n):
        lo = max(0, i - window + 1)
        hi = i + 1
        result[i] = (cumsum[hi] - cumsum[lo]) / (hi - lo)
    return result


def _rolling_max(arr: np.ndarray, window: int) -> np.ndarray:
    """Compute rolling maximum over a fixed window (O(n) via deque)."""
    from collections import deque

    n = len(arr)
    result = np.full(n, np.nan, dtype=np.float64)
    dq = deque()  # stores indices, values decreasing
    for i in range(n):
        while dq and arr[dq[-1]] <= arr[i]:
            dq.pop()
        dq.append(i)
        if dq[0] <= i - window:
            dq.popleft()
        if i >= window - 1:
            result[i] = float(arr[dq[0]])
    return result


def _compute_normalized_power(power: np.ndarray) -> float:
    """
    Compute Normalized Power via the 4th-power method with 30-second moving average.

    NP = ( sum( np_ma^4 * dt ) / sum(dt) ) ^ 0.25
    where np_ma is the 30-second moving average of the 4th power of raw power,
    and dt=1 second.
    """
    if len(power) == 0:
        return 0.0

    # 4th power of each sample
    p4 = np.power(power, 4)

    # 30-second moving average of the 4th powers
    p4_ma = _moving_average(p4, 30)

    # NP = (mean of 30s-MA of p^4) ^ 0.25
    np_val = float(np.mean(p4_ma)) ** 0.25
    return np_val


def _compute_time_in_zones(power: np.ndarray, ftp: float) -> dict[str, float]:
    """
    Compute time spent in each Coggan 5-zone as a percentage of total time.

    Zones (fraction of FTP):
      Z1: <  56%
      Z2:  56-75%
      Z3:  76-90%
      Z4:  91-105%
      Z5:  > 105%
    """
    # Filter out zero-power samples
    valid = [p for p in power if p > 0]
    n = len(valid)
    if n == 0:
        return {zone: 0.0 for zone in _ZONE_NAMES}

    # Fraction of FTP for each valid sample
    fractions = np.array(valid) / ftp if ftp > 0 else np.zeros(len(valid))

    zone_counts = np.zeros(5, dtype=np.float64)
    for frac in fractions:
        if frac < _ZONE_BOUNDARIES[1]:
            zone_counts[0] += 1
        elif frac < _ZONE_BOUNDARIES[2]:
            zone_counts[1] += 1
        elif frac < _ZONE_BOUNDARIES[3]:
            zone_counts[2] += 1
        elif frac < _ZONE_BOUNDARIES[4]:
            zone_counts[3] += 1
        else:
            zone_counts[4] += 1

    return {
        zone: float(count / n * 100)
        for zone, count in zip(_ZONE_NAMES, zone_counts)
    }

def _compute_power_duration_curve(power: np.ndarray) -> dict[int, float]:
    """
    Compute the best N-second average power for a set of standard durations.

    Uses rolling max of 1-second power, then averages over the window.
    Filters out zero-power samples before computing rolling averages.
    """
    # Filter out zero-power samples
    valid = np.array([p for p in power if p > 0])
    n = len(valid)
    curve: dict[int, float] = {}

    for dur in _PDC_DURATIONS:
        if dur > n:
            curve[dur] = 0.0
            continue
        # Rolling mean over `dur` seconds; take the max
        # Use cumsum for O(n) rolling mean
        cumsum = np.cumsum(valid)
        cumsum = np.insert(cumsum, 0, 0.0)
        window_sums = cumsum[dur:] - cumsum[:-dur]
        best_avg = float(np.max(window_sums) / dur)
        curve[dur] = best_avg

    return curve

def compute_power_metrics(
    activity_id: str,
    power_samples: list[float],
    duration: float,
    ftp: float,
) -> PowerMetricsResult:
    """
    Compute power metrics for a single activity.

    Args:
        activity_id: Unique identifier for the activity.
        power_samples: Power in watts at 1-second intervals.
        duration: Total duration in seconds.
        ftp: Functional Threshold Power in watts.

    Returns:
        PowerMetricsResult with all computed metrics.
    """
    power = np.array(power_samples, dtype=np.float64)
    n = len(power)

    if n == 0 or ftp <= 0:
        result = PowerMetricsResult(
            activity_id=activity_id,
            normalized_power=0.0,
            intensity_factor=0.0,
            tss=0.0,
            variability_index=0.0,
            time_in_zones={zone: 0.0 for zone in _ZONE_NAMES},
            power_duration_curve={dur: 0.0 for dur in _PDC_DURATIONS},
        )
        logger.warning(f"No power data or invalid FTP for {activity_id}")
        return result

    avg_power = float(np.mean(power))

    # Normalized Power
    np_val = _compute_normalized_power(power)

    # Intensity Factor
    if_val = np_val / ftp

    # TSS = duration_sec * NP * IF / (FTP * 3600) * 100
    # Since IF = NP/FTP, this is equivalent to (NP^2 / FTP^2) * (duration_hr) * 100
    tss_val = duration * np_val * if_val / (ftp * 3600) * 100

    # Variability Index
    vi_val = avg_power / np_val if np_val > 0 else 0.0

    # Time in zones
    zones = _compute_time_in_zones(power, ftp)

    # Power duration curve
    pdc = _compute_power_duration_curve(power)

    logger.info(
        f"Power metrics for {activity_id}: NP={np_val:.1f}W, IF={if_val:.2f}, "
        f"TSS={tss_val:.1f}, VI={vi_val:.2f}"
    )

    return PowerMetricsResult(
        activity_id=activity_id,
        normalized_power=round(np_val, 2),
        intensity_factor=round(if_val, 4),
        tss=round(tss_val, 2),
        variability_index=round(vi_val, 4),
        time_in_zones={zone: round(pct, 2) for zone, pct in zones.items()},
        power_duration_curve={dur: round(watts, 2) for dur, watts in pdc.items()},
    )


def estimate_critical_power(activity_data: list[dict]) -> float:
    """
    Estimate Critical Power using a 2-parameter model from multiple activities.

    For each activity, compute (duration, avg_power) then fit:
        W = CP * t + W'
    where W = avg_power * duration (total work in joules).

    Rearranging:  avg_power = CP + W' / duration
    So plotting avg_power vs 1/duration gives a line with intercept = CP.

    Only uses activities with duration >= 60s and avg_power > 0.

    Args:
        activity_data: List of dicts, each with keys:
            - 'duration': float, duration in seconds
            - 'power_samples': list[float], per-second power in watts
            - (optional) 'avg_power': float, precomputed average power

    Returns:
        Estimated Critical Power in watts. Returns 0.0 if insufficient data.
    """
    points: list[tuple[float, float]] = []  # (1/duration, avg_power)

    for activity in activity_data:
        duration = float(activity.get("duration", 0))
        if duration < 60:
            continue

        # Get average power from precomputed value or compute from samples
        avg_power = activity.get("avg_power")
        if avg_power is None:
            samples = activity.get("power_samples", [])
            if not samples:
                continue
            avg_power = float(np.mean(samples))

        if avg_power <= 0:
            continue

        points.append((1.0 / duration, avg_power))

    if len(points) < 2:
        logger.warning("Insufficient data for CP estimation (need >= 2 activities)")
        return 0.0

    x = np.array([p[0] for p in points])  # 1/duration
    y = np.array([p[1] for p in points])  # avg_power

    # Linear regression: y = CP + W' * x  =>  intercept is CP
    # Use numpy polyfit for robustness
    coeffs = np.polyfit(x, y, 1)  # coeffs[0] = slope (W'), coeffs[1] = intercept (CP)
    cp = float(coeffs[1])

    # Sanity: CP should be positive and less than the max observed power
    if cp <= 0:
        logger.warning(f"CP estimate non-positive ({cp}), returning 0")
        return 0.0

    max_observed = float(np.max(y))
    if cp >= max_observed:
        logger.warning(
            f"CP estimate ({cp:.1f}) >= max observed power ({max_observed:.1f}), "
            "clamping to 95% of max"
        )
        cp = max_observed * 0.95

    logger.info(f"Estimated Critical Power: {cp:.1f}W from {len(points)} activities")
    return round(cp, 2)


def power_metrics_to_dict(result: PowerMetricsResult) -> dict[str, Any]:
    """Serialize PowerMetricsResult to a plain dict."""
    return {
        "activity_id": result.activity_id,
        "normalized_power": result.normalized_power,
        "intensity_factor": result.intensity_factor,
        "tss": result.tss,
        "variability_index": result.variability_index,
        "time_in_zones": result.time_in_zones,
        "power_duration_curve": result.power_duration_curve,
    }