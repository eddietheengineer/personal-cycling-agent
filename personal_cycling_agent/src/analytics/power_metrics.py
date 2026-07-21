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
- CP model: FastFitness.Tips / Morton's 3-parameter CP model (fft.tips/curve)
- NP: TrainingPeaks/Hunter Allen 4th-power method
- Zones: Coggan (2015) 5-zone model
- TSS/IF: Banister et al. (1999) impulse-response model
"""

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
try:
    from src.config.constants import NP_FOURTH_ROOT, NP_POWER_EXPONENT
except ImportError:
    from ..config.constants import NP_FOURTH_ROOT, NP_POWER_EXPONENT

logger = logging.getLogger(__name__)

# Coggan 5-zone FTP boundaries (fractional)
_ZONE_BOUNDARIES = [0.0, 0.56, 0.75, 0.90, 1.05, float("inf")]
_ZONE_NAMES = ["Z1", "Z2", "Z3", "Z4", "Z5"]

# FastFitness.Tips power curve ratios (All-rounder profile).
# Maps duration (seconds) -> fraction of FTP. Source: fft.tips/curve,
# based on Morton's 3-parameter CP model fitted to ~5000 cyclists.
# intervals.icu uses these same ratios for eFTP estimation.
# Log-log interpolation between table entries gives ratios for any duration.
_FTT_CP_RATIOS: dict[int, float] = {
    60: 1.808,    # 1 min: Neuromuscular Sprint
    120: 1.503,   # 2 min: Sprint
    180: 1.400,   # 3 min
    300: 1.281,   # 5 min: VO2Max & 4DP
    480: 1.225,   # 8 min: TrainerRoad 8min
    600: 1.201,   # 10 min
    900: 1.158,   # 15 min
    1200: 1.128,  # 20 min: Zwift ShortFTP
    1800: 1.079,  # 30 min
    3600: 1.000,  # 60 min: FTP60
}
# Minimum effort duration for CP estimation (seconds). Below this, power is
# dominated by anaerobic capacity (PP/W'), not CP. Aligns with intervals.icu default.
_CP_MIN_DURATION = 180
# Maximum effort duration for CP estimation (seconds). Above this, power drops
# below FTP due to fatigue (endurance fade), not threshold capacity.
_CP_MAX_DURATION = 3600
# Power-duration curve durations in seconds
# Fine granularity in CP-sensitive range: every 10s from 3-10min, every 1min from 10-60min.
_PDC_DURATIONS = sorted(set(
    [1, 3, 5, 10, 30, 60, 120]
    + list(range(180, 610, 10))
    + list(range(600, 3601, 60))
))


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
    np_val = float(np.mean(np.power(p_ma, NP_POWER_EXPONENT))) ** NP_FOURTH_ROOT
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

    Uses rolling averages over the entire ride (including zero-power segments).
    A 20-minute average that includes stops is still a valid effort — it tells
    us about sustained capacity over that time period.
    """
    n = len(power)
    curve: dict[int, float] = {}

    if n == 0:
        for dur in _PDC_DURATIONS:
            curve[dur] = 0.0
        return curve

    # Filter out obviously bad power samples (>2000W = sensor glitch/overflow)
    power = np.where(power > 2000, 0.0, power)

    # Cumulative sum for efficient rolling window computation
    cumsum = np.cumsum(power)
    cumsum = np.insert(cumsum, 0, 0.0)

    for dur in _PDC_DURATIONS:
        if n < dur:
            curve[dur] = 0.0
            continue
        window_sums = cumsum[dur:] - cumsum[:-dur]
        curve[dur] = float(np.max(window_sums) / dur)

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


def _ftt_ratio(dur: float) -> float:
    """
    Interpolate the FastFitness.Tips power curve ratio for a given duration.

    Uses log-log interpolation between table entries. Ratios represent
    effort_power / FTP for the All-rounder profile.

    Source: fft.tips/curve, Morton's 3-parameter CP model fitted to ~5000 cyclists.
    """
    durations = sorted(_FTT_CP_RATIOS.keys())
    if dur <= durations[0]:
        return _FTT_CP_RATIOS[durations[0]]
    if dur >= durations[-1]:
        return _FTT_CP_RATIOS[durations[-1]]

    for i in range(len(durations) - 1):
        if durations[i] <= dur <= durations[i + 1]:
            log_d = math.log(dur)
            log_d0 = math.log(durations[i])
            log_d1 = math.log(durations[i + 1])
            log_r0 = math.log(_FTT_CP_RATIOS[durations[i]])
            log_r1 = math.log(_FTT_CP_RATIOS[durations[i + 1]])
            t = (log_d - log_d0) / (log_d1 - log_d0)
            log_r = log_r0 + t * (log_r1 - log_r0)
            return math.exp(log_r)
    return 1.0


def _estimate_cp_from_efforts(
    efforts: list[tuple[float, float]],
) -> tuple[float, float]:
    """
    Estimate CP from a list of (duration, avg_power) efforts using FTT ratios.

    For each effort, CP = power / ratio(duration). Takes the maximum CP
    across all efforts — the best effort in the window defines CP.

    Returns (cp, w_prime) tuple. W' is estimated from the best effort:
    W' = (power - CP) * duration.

    Returns (0.0, 0.0) if no valid efforts found.
    """
    if not efforts:
        return 0.0, 0.0

    best_cp = 0.0
    best_effort: tuple[float, float] | None = None

    for dur, pwr in efforts:
        if dur < _CP_MIN_DURATION or dur > _CP_MAX_DURATION or pwr <= 0:
            continue
        ratio = _ftt_ratio(dur)
        cp = pwr / ratio
        if cp > best_cp:
            best_cp = cp
            best_effort = (dur, pwr)

    if best_cp <= 0 or best_effort is None:
        return 0.0, 0.0

    # Estimate W' from the best effort: W' = (power - CP) * duration
    w_prime = (best_effort[1] - best_cp) * best_effort[0]
    if w_prime <= 0:
        w_prime = 0.0

    return round(best_cp, 2), round(w_prime, 2)


def estimate_critical_power(
    activity_data: list[dict],
) -> tuple[float, float]:
    """
    Estimate Critical Power and W' from PDC best-effort data using
    FastFitness.Tips power curve ratios (Morton's 3-parameter CP model).

    For each activity, extracts best sustained power at all available durations
    from the power-duration curve. For each effort, CP = power / ratio(duration).
    The maximum CP across all efforts defines the estimate.

    Only uses efforts with duration between {_CP_MIN_DURATION}s and {_CP_MAX_DURATION}s
    (3min to 60min). Below 3min, power is dominated by anaerobic capacity.
    Above 60min, power drops due to endurance fade, not threshold capacity.

    Source: FastFitness.Tips (fft.tips/curve), based on Morton's 3-parameter
    critical power model fitted to ~5000 cyclists. Same approach used by
    intervals.icu for eFTP estimation.

    Args:
        activity_data: List of dicts, each with keys:
            'power_duration_curve': dict[int, float], best N-sec power (from PDC)
            'pdc_efforts': list[dict] with 'duration' and 'avg_power' (pre-extracted)
            (fallback) 'duration': float, 'avg_power': float (whole-ride, legacy)

    Returns:
        (cp, w_prime) tuple in watts and joules respectively.
        Returns (0.0, 0.0) if insufficient data.
    """
    # Collect best effort at each duration across all rides
    best_by_duration: dict[int, float] = {}

    for activity in activity_data:
        pdc_efforts = activity.get("pdc_efforts", [])
        if not pdc_efforts:
            pdc = activity.get("power_duration_curve", {})
            if pdc:
                for dur, pwr in pdc.items():
                    if dur >= _CP_MIN_DURATION and dur <= _CP_MAX_DURATION and pwr > 0:
                        pdc_efforts.append({"duration": dur, "avg_power": pwr})
            else:
                # Legacy fallback: whole-ride average
                duration = float(activity.get("duration", 0))
                avg_power = activity.get("avg_power")
                if avg_power is None:
                    samples = activity.get("power_samples", [])
                    if samples:
                        avg_power = float(np.mean(samples))
                if (duration >= _CP_MIN_DURATION
                        and duration <= _CP_MAX_DURATION
                        and avg_power and avg_power > 0):
                    pdc_efforts.append({"duration": duration, "avg_power": avg_power})

        for effort in pdc_efforts:
            duration = float(effort.get("duration", 0))
            avg_power = float(effort.get("avg_power", 0))
            if duration < _CP_MIN_DURATION or duration > _CP_MAX_DURATION:
                continue
            if avg_power <= 0:
                continue
            dur_key = int(duration)
            if dur_key not in best_by_duration or avg_power > best_by_duration[dur_key]:
                best_by_duration[dur_key] = avg_power

    efforts = list(best_by_duration.items())
    cp, w_prime = _estimate_cp_from_efforts(efforts)

    if cp > 0:
        logger.info(
            f"Estimated CP: {cp:.1f}W, W': {w_prime:.0f}J from {len(efforts)} efforts"
        )
    return cp, w_prime


def estimate_ride_cp(pdc: dict[int, float]) -> float | None:
    """
    Estimate Critical Power for a single ride from its PDC using FTT ratios.

    For each effort >= 180s, computes CP = power / ratio(duration).
    Takes the maximum CP across all efforts.

    Returns None if no usable data found.
    """
    efforts: list[tuple[float, float]] = []
    for dur, pwr in pdc.items():
        if dur >= _CP_MIN_DURATION and dur <= _CP_MAX_DURATION and pwr > 0:
            efforts.append((float(dur), float(pwr)))

    if not efforts:
        return None

    cp, _ = _estimate_cp_from_efforts(efforts)
    if cp > 50:
        return cp
    return None


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