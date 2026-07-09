"""
Durability Profiling.

Calculates multi-state Power-Duration Curves (PDC) and tracks
1-minute and 5-minute peak power at different cumulative kJ loads
to quantify structural endurance degradation.
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Fatigue load thresholds in kJ
FRESH_KJ = 0
FATIGUED_KJ = 1000
DEEPLY_FATIGUED_KJ = 1500


@dataclass
class DurabilityProfile:
    """Durability metrics for a single activity."""

    activity_id: str
    total_kj: float
    # Peak 1-min power at different fatigue states
    peak_1min_fresh: float | None
    peak_1min_fatigued: float | None
    peak_1min_deeply_fatigued: float | None
    # Peak 5-min power at different fatigue states
    peak_5min_fresh: float | None
    peak_5min_fatigued: float | None
    peak_5min_deeply_fatigued: float | None
    # Degradation ratios (fatigued/fresh, deeply_fatigued/fresh)
    degradation_1min: float | None
    degradation_5min: float | None


def compute_durability(
    activity_id: str,
    power_samples: list[float],
    durations: list[int] | None = None,
) -> DurabilityProfile:
    """
    Compute durability metrics from power data.

    Tracks peak 1-min and 5-min power at 0 kJ (fresh), 1000 kJ (fatigued),
    and 1500 kJ (deeply fatigued) cumulative energy expenditure.

    Args:
        activity_id: Intervals.icu activity ID.
        power_samples: Power in watts at 1-second intervals.
        durations: Optional list of interval durations in seconds.

    Returns:
        DurabilityProfile with peak powers at each fatigue state.
    """
    if not power_samples:
        return DurabilityProfile(
            activity_id=activity_id,
            total_kj=0.0,
            peak_1min_fresh=None,
            peak_1min_fatigued=None,
            peak_1min_deeply_fatigued=None,
            peak_5min_fresh=None,
            peak_5min_fatigued=None,
            peak_5min_deeply_fatigued=None,
            degradation_1min=None,
            degradation_5min=None,
        )

    power = np.array(power_samples)
    n = len(power)

    # Compute cumulative kJ (integral of power over time, assuming 1s intervals)
    cumulative_kj = np.cumsum(power) / 1000.0
    total_kj = float(cumulative_kj[-1])

    # Rolling max for 1-min (60s) and 5-min (300s) windows
    from collections import deque

    def rolling_max(arr: np.ndarray, window: int) -> np.ndarray:
        """Compute rolling maximum over a fixed window using deque (O(n))."""
        result = np.full_like(arr, np.nan)
        dq = deque()  # stores indices, values decreasing
        for i in range(len(arr)):
            while dq and arr[dq[-1]] <= arr[i]:
                dq.pop()
            dq.append(i)
            if dq[0] <= i - window:
                dq.popleft()
            if i >= window:
                result[i] = float(arr[dq[0]])
        return result

    window_1min = 60
    window_5min = 300

    peak_1min = rolling_max(power, window_1min)
    peak_5min = rolling_max(power, window_5min)

    # Find the first sample where cumulative kJ crosses each threshold
    def peak_at_load(
        peaks: np.ndarray, cum_kj: np.ndarray, threshold: float
    ) -> float | None:
        """Get the rolling peak value at the point where cumulative kJ crosses threshold."""
        idx = np.searchsorted(cum_kj, threshold)
        if idx >= len(peaks) or np.isnan(peaks[idx]):
            return None
        return float(peaks[idx])

    p1_fresh = peak_at_load(peak_1min, cumulative_kj, FRESH_KJ + 1)  # near start
    p1_fatigued = peak_at_load(peak_1min, cumulative_kj, FATIGUED_KJ)
    p1_deep = peak_at_load(peak_1min, cumulative_kj, DEEPLY_FATIGUED_KJ)

    p5_fresh = peak_at_load(peak_5min, cumulative_kj, FRESH_KJ + 1)
    p5_fatigued = peak_at_load(peak_5min, cumulative_kj, FATIGUED_KJ)
    p5_deep = peak_at_load(peak_5min, cumulative_kj, DEEPLY_FATIGUED_KJ)

    # Degradation ratios
    deg_1min = (p1_fatigued / p1_fresh * 100) if (p1_fresh and p1_fatigued) else None
    deg_5min = (p5_fatigued / p5_fresh * 100) if (p5_fresh and p5_fatigued) else None

    logger.info(
        f"Durability for {activity_id}: total_kj={total_kj:.1f}, "
        f"1min degradation={deg_1min}, 5min degradation={deg_5min}"
    )

    return DurabilityProfile(
        activity_id=activity_id,
        total_kj=round(total_kj, 2),
        peak_1min_fresh=p1_fresh,
        peak_1min_fatigued=p1_fatigued,
        peak_1min_deeply_fatigued=p1_deep,
        peak_5min_fresh=p5_fresh,
        peak_5min_fatigued=p5_fatigued,
        peak_5min_deeply_fatigued=p5_deep,
        degradation_1min=round(deg_1min, 2) if deg_1min else None,
        degradation_5min=round(deg_5min, 2) if deg_5min else None,
    )


def durability_to_dict(result: DurabilityProfile) -> dict[str, Any]:
    """Serialize DurabilityProfile to a plain dict."""
    return {
        "activity_id": result.activity_id,
        "total_kj": result.total_kj,
        "peak_1min_fresh": result.peak_1min_fresh,
        "peak_1min_fatigued": result.peak_1min_fatigued,
        "peak_1min_deeply_fatigued": result.peak_1min_deeply_fatigued,
        "peak_5min_fresh": result.peak_5min_fresh,
        "peak_5min_fatigued": result.peak_5min_fatigued,
        "peak_5min_deeply_fatigued": result.peak_5min_deeply_fatigued,
        "degradation_1min": result.degradation_1min,
        "degradation_5min": result.degradation_5min,
    }