"""
Cycling Power Metrics.

Computes normalized power, intensity factor, TSS, variability index,
time-in-zones, and power-duration curves from per-second power samples.
Also estimates Critical Power from a history of activities.

Conventions:
- Power samples are 1-second intervals (dt=1).
- Coggan 5-zone model for time-in-zones.
- 4th-power NP with 30-second moving average (Intervals.icu method).

Sources:
- CP model: Monod & Scherrer (1965), Hill et al. (1999) — 2-parameter P = CP + W'/t
- NP: TrainingPeaks/Hunter Allen 4th-power method
- Zones: Coggan (2015) 5-zone model
- TSS/IF: Banister et al. (1999) impulse-response model
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Coggan 5-zone FTP boundaries (fractional)
_ZONE_BOUNDARIES = [0.0, 0.56, 0.75, 0.90, 1.05, float("inf")]
_ZONE_NAMES = ["Z1", "Z2", "Z3", "Z4", "Z5"]

# Durations used for CP estimation from PDC best-effort powers (seconds)
# 3min, 5min, 8min, 20min — covers the CP-sensitive range per Monod-Scherrer
# Shorter efforts are dominated by PP/W'; longer efforts approach the CP asymptote.
_CP_ESTIMATION_DURATIONS = [180, 300, 480, 1200]
# Minimum effort duration for CP estimation (seconds). Below this, power is
# dominated by anaerobic capacity (PP/W'), not CP. Aligns with intervals.icu default.
_CP_MIN_DURATION = 180
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

    The correct formula (matching Intervals.icu and TrainingPeaks) is:
      1. Compute a 30-second moving average of raw power
      2. Raise each averaged value to the 4th power
      3. Take the mean of those 4th powers
      4. Take the 4th root

    NP = ( mean( MA_30s(power)^4 ) ) ^ 0.25

    This is NOT the same as mean(MA(power^4))^0.25 — taking the MA of
    the 4th powers before averaging massively overweights brief spikes
    (e.g. a single 874W spike in a 30s window of 100W would contribute
    ~374W to NP instead of the correct ~126W).
    """
    if len(power) == 0:
        return 0.0

    # 30-second moving average of raw power
    p_ma = _moving_average(power, 30)

    # Raise to 4th power, take mean, then 4th root
    np_val = float(np.mean(np.power(p_ma, 4))) ** 0.25
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

    Finds contiguous segments of positive power (>0W), then computes the best
    rolling average within each segment. This prevents zero-power gaps (stops,
    rest) from being stitched together into artificially high averages.
    """
    n = len(power)
    curve: dict[int, float] = {}

    if n == 0:
        for dur in _PDC_DURATIONS:
            curve[dur] = 0.0
        return curve

    # Find contiguous segments of positive power
    segments: list[tuple[int, int]] = []  # (start, end) exclusive
    in_segment = False
    seg_start = 0
    for i in range(n):
        if power[i] > 0 and not in_segment:
            in_segment = True
            seg_start = i
        elif power[i] == 0 and in_segment:
            in_segment = False
            segments.append((seg_start, i))
    if in_segment:
        segments.append((seg_start, n))

    for dur in _PDC_DURATIONS:
        best = 0.0
        for seg_start, seg_end in segments:
            seg_len = seg_end - seg_start
            if seg_len < dur:
                continue
            seg = power[seg_start:seg_end]
            cumsum = np.cumsum(seg)
            cumsum = np.insert(cumsum, 0, 0.0)
            window_sums = cumsum[dur:] - cumsum[:-dur]
            seg_best = float(np.max(window_sums) / dur)
            if seg_best > best:
                best = seg_best
        curve[dur] = best

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


def estimate_critical_power(
    activity_data: list[dict],
) -> tuple[float, float]:
    """
    Estimate Critical Power and W' using a 2-parameter model from PDC best-effort data.

    For each activity, extracts the best sustained power at standard durations
    (3min, 5min, 8min, 20min) from the power-duration curve. These represent
    the athlete's true threshold capacity at each duration, unlike whole-ride
    averages which dilute short hard efforts.

    Model: avg_power = CP + W' / duration
    Plotting avg_power vs 1/duration gives a line with intercept = CP, slope = W'.

    Uses weighted least squares where weight = duration (longer efforts have
    lower variance in their power estimate).

    Only uses efforts with duration >= {_CP_MIN_DURATION}s and avg_power > 0.

    Source: Monod & Scherrer (1965) 2-parameter CP model; weighted LS per
    standard regression practice (longer efforts have lower variance).

    Args:
        activity_data: List of dicts, each with keys:
            - 'power_duration_curve': dict[int, float], best N-sec power (from PDC)
            - 'pdc_efforts': list[dict] with 'duration' and 'avg_power' (pre-extracted)
            - (fallback) 'duration': float, 'avg_power': float (whole-ride, legacy)

    Returns:
        (cp, w_prime) tuple in watts and joules respectively.
        Returns (0.0, 0.0) if insufficient data.
    """
    points: list[tuple[float, float, float]] = []  # (1/duration, avg_power, weight)

    # Collect best effort at each duration across all rides
    best_by_duration: dict[int, float] = {}

    for activity in activity_data:
        # Try PDC efforts first (best-effort at standard durations)
        pdc_efforts = activity.get("pdc_efforts", [])
        if not pdc_efforts:
            # Fallback: extract from power_duration_curve if available
            pdc = activity.get("power_duration_curve", {})
            if pdc:
                for dur in _CP_ESTIMATION_DURATIONS:
                    pwr = pdc.get(dur, 0)
                    if dur >= _CP_MIN_DURATION and pwr > 0:
                        pdc_efforts.append({"duration": dur, "avg_power": pwr})
            else:
                # Legacy fallback: whole-ride average (less accurate)
                duration = float(activity.get("duration", 0))
                avg_power = activity.get("avg_power")
                if avg_power is None:
                    samples = activity.get("power_samples", [])
                    if samples:
                        avg_power = float(np.mean(samples))
                if duration >= _CP_MIN_DURATION and avg_power and avg_power > 0:
                    pdc_efforts.append({"duration": duration, "avg_power": avg_power})

        for effort in pdc_efforts:
            duration = float(effort.get("duration", 0))
            avg_power = float(effort.get("avg_power", 0))

            if duration < _CP_MIN_DURATION:
                continue
            if avg_power <= 0:
                continue

            # Keep only the best power at each duration
            dur_key = int(duration)
            if dur_key not in best_by_duration or avg_power > best_by_duration[dur_key]:
                best_by_duration[dur_key] = avg_power

    # Build regression points from best efforts
    for duration, avg_power in best_by_duration.items():
        weight = duration
        points.append((1.0 / duration, avg_power, weight))

    if len(points) < 2:
        logger.warning(
            "Insufficient data for CP estimation (need >= 2 efforts >= %ds)",
            _CP_MIN_DURATION,
        )
        return 0.0, 0.0

    x = np.array([p[0] for p in points])  # 1/duration
    y = np.array([p[1] for p in points])  # avg_power
    w = np.array([p[2] for p in points])  # weights

    # Weighted linear regression: y = CP + W' * x
    # Normal equations with weights: (W^T W) beta = W^T y
    # Source: Standard weighted least squares; weight = duration (longer efforts
    # have lower variance in their power estimate per Monod-Scherrer model).
    sw = np.sum(w)
    swx = np.sum(w * x)
    swx2 = np.sum(w * x * x)
    swy = np.sum(w * y)
    swxy = np.sum(w * x * y)

    denom = sw * swx2 - swx * swx
    if abs(denom) < 1e-12:
        logger.warning("Singular weighted regression matrix in CP estimation")
        return 0.0, 0.0

    slope = (sw * swxy - swx * swy) / denom       # W'
    intercept = (swx2 * swy - swx * swxy) / denom  # CP

    cp = float(intercept)
    w_prime = float(slope)  # in joules (W * s = J)

    # Sanity: CP should be positive and less than the max observed power
    if cp <= 0:
        logger.warning(f"CP estimate non-positive ({cp}), returning 0")
        return 0.0, 0.0

    max_observed = float(np.max(y))
    if cp >= max_observed:
        logger.warning(
            f"CP estimate ({cp:.1f}) >= max observed power ({max_observed:.1f}), "
            "clamping to 95% of max"
        )
        cp = max_observed * 0.95

    # Sanity: W' should be positive
    if w_prime <= 0:
        logger.warning(f"W' estimate non-positive ({w_prime}), returning 0")
        w_prime = 0.0

    logger.info(
        f"Estimated CP: {cp:.1f}W, W': {w_prime:.0f}J from {len(points)} efforts"
    )
    return round(cp, 2), round(w_prime, 2)


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