"""
Aerobic Decoupling (Pw:HR) Analysis.

Tracks cardiac drift by comparing the Power-to-Heart-Rate ratio
between the first and second halves of steady-state aerobic rides.

- Drift > 5%: maintain current volume.
- Drift < 5%: green light to increase interval duration.
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
try:
    from src.config.constants import DECOUPLING_DRIFT_THRESHOLD_PCT, DECOUPLING_MIN_SAMPLES
except ImportError:
    from ..config.constants import DECOUPLING_DRIFT_THRESHOLD_PCT, DECOUPLING_MIN_SAMPLES

logger = logging.getLogger(__name__)


@dataclass
class DecouplingResult:
    """Decoupling analysis for a single activity."""

    activity_id: str
    first_half_pw_hr: float  # avg power/HR ratio in first half
    second_half_pw_hr: float  # avg power/HR ratio in second half
    drift_pct: float  # percentage drift (negative = HR increased relative to power)
    increase_duration_recommended: bool  # True if drift < 5%


def compute_decoupling(
    activity_id: str,
    power_samples: list[float],
    hr_samples: list[float],
    drift_threshold: float = DECOUPLING_DRIFT_THRESHOLD_PCT,
) -> DecouplingResult:
    """
    Compute aerobic decoupling from power and heart rate time series.

    Splits the activity into two halves, computes the average Pw:HR ratio
    for each, and calculates the percentage drift.

    Args:
        activity_id: Intervals.icu activity ID.
        power_samples: Power in watts at 1-second intervals.
        hr_samples: Heart rate in bpm at 1-second intervals (aligned with power).
        drift_threshold: Max acceptable drift in percent.

    Returns:
        DecouplingResult with drift metrics and recommendation.
    """
    # Trim to the shorter array length (FIT files may have slightly different sample counts)
    min_len = min(len(power_samples), len(hr_samples))
    if min_len < DECOUPLING_MIN_SAMPLES:
        return DecouplingResult(
            activity_id=activity_id,
            first_half_pw_hr=0.0,
            second_half_pw_hr=0.0,
            drift_pct=0.0,
            increase_duration_recommended=False,
        )
    power_samples = power_samples[:min_len]
    hr_samples = hr_samples[:min_len]

    if not power_samples or not hr_samples:
        return DecouplingResult(
            activity_id=activity_id,
            first_half_pw_hr=0.0,
            second_half_pw_hr=0.0,
            drift_pct=0.0,
            increase_duration_recommended=False,
        )

    power = np.array(power_samples)
    hr = np.array(hr_samples)

    n = len(power)
    mid = n // 2

    # Filter out zero/invalid values
    first_mask = (power[:mid] > 0) & (hr[:mid] > 0)
    second_mask = (power[mid:] > 0) & (hr[mid:] > 0)

    if not np.any(first_mask) or not np.any(second_mask):
        return DecouplingResult(
            activity_id=activity_id,
            first_half_pw_hr=0.0,
            second_half_pw_hr=0.0,
            drift_pct=0.0,
            increase_duration_recommended=False,
        )

    first_half_ratio = float(np.mean(power[:mid][first_mask] / hr[:mid][first_mask]))
    second_half_ratio = float(np.mean(power[mid:][second_mask] / hr[mid:][second_mask]))

    # Drift: how much the ratio changed (negative = HR went up relative to power = bad)
    if first_half_ratio > 0:
        drift_pct = ((second_half_ratio - first_half_ratio) / first_half_ratio) * 100.0
    else:
        drift_pct = 0.0

    # If drift is small (absolute), aerobic fitness is holding.
    # Negative drift (HR rising) is bad — don't increase duration.
    # Positive drift (HR falling) is good — can increase duration.
    if drift_pct < 0:
        # HR rising — fatigue. Only recommend increase if drift is small.
        increase_duration = abs(drift_pct) < drift_threshold
    else:
        # HR falling — fitness improving. Always recommend increase.
        increase_duration = True

    logger.info(
        f"Decoupling for {activity_id}: "
        f"1st_half={first_half_ratio:.2f}, 2nd_half={second_half_ratio:.2f}, "
        f"drift={drift_pct:.2f}%, increase_duration={increase_duration}"
    )

    return DecouplingResult(
        activity_id=activity_id,
        first_half_pw_hr=round(first_half_ratio, 4),
        second_half_pw_hr=round(second_half_ratio, 4),
        drift_pct=round(drift_pct, 4),
        increase_duration_recommended=increase_duration,
    )


def decoupling_to_dict(result: DecouplingResult) -> dict[str, Any]:
    """Serialize DecouplingResult to a plain dict."""
    return {
        "activity_id": result.activity_id,
        "first_half_pw_hr": result.first_half_pw_hr,
        "second_half_pw_hr": result.second_half_pw_hr,
        "drift_pct": result.drift_pct,
        "increase_duration_recommended": result.increase_duration_recommended,
    }