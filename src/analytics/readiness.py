"""
Two-Factor Autonomous Readiness Engine.

Calculates 30-day rolling baselines for RMSSD (HRV) and RHR,
then classifies the rider's current autonomic state:
- Coping (Green)
- Sympathetic Stress (Red/Yellow)
- Parasympathetic Hyperactivity (Yellow)
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class ReadinessState(Enum):
    COPING = "coping"
    SYMPATHETIC_STRESS = "sympathetic_stress"
    PARASYMPATHETIC_HYPERACTIVITY = "parasympathetic_hyperactivity"


@dataclass
class ReadinessResult:
    """Output of the readiness analysis for a single day."""

    date: str
    rmssd: float | None
    resting_hr: float | None
    rmssd_mean: float
    rmssd_std: float
    rhr_mean: float
    rhr_std: float
    rmssd_lower_band: float
    rmssd_upper_band: float
    rhr_lower_band: float
    rhr_upper_band: float
    state: ReadinessState
    recommendation: str
    confidence: str


def _compute_bands(
    values: list[float], window: int = 30
) -> tuple[float, float, float, float]:
    """
    Compute rolling mean, std, and normal bands (mean ± 0.75*std).

    Returns (mean, std, lower, upper).
    """
    arr = np.array(values)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    band = 0.75 * std
    lower = mean - band
    upper = mean + band
    return mean, std, lower, upper


def assess_readiness(
    wellness_records: list[dict[str, Any]],
    target_date: str | None = None,
    window: int = 30,
) -> ReadinessResult:
    """
    Assess readiness for a target date based on recent wellness history.

    Args:
        wellness_records: List of wellness dicts (from Garmin Connect or DB).
            Each must have 'date' (date string), 'rmssd', and 'resting_hr'.
        target_date: ISO date string to assess. Defaults to the most recent record.
        window: Number of days for rolling baseline.

    Returns:
        ReadinessResult with state classification and recommendation.
    """
    if not wellness_records:
        raise ValueError("No wellness records provided")

    # Sort by date descending
    records = sorted(wellness_records, key=lambda r: r.get("date", ""), reverse=True)

    if not target_date:
        target_date = records[0].get("date", "")

    # Find today's values
    today = next((r for r in records if r.get("date") == target_date), None)
    if today is None:
        raise ValueError(f"No wellness record found for date {target_date}")

    rmssd = today.get("rmssd")
    resting_hr = today.get("resting_hr")

    if rmssd is None or resting_hr is None:
        raise ValueError(
            f"RMSSD and resting_hr are required for readiness assessment on {target_date}"
        )

    # Build baseline from the N days BEFORE today (not including today)
    baseline_records = [
        r for r in records if r.get("date", "") < target_date
    ][:window]

    rmssd_values = [
        r["rmssd"] for r in baseline_records if r.get("rmssd") is not None
    ]
    rhr_values = [
        r["resting_hr"] for r in baseline_records if r.get("resting_hr") is not None
    ]

    if len(rmssd_values) < 7 or len(rhr_values) < 7:
        logger.warning(
            f"Insufficient baseline data: {len(rmssd_values)} RMSSD, {len(rhr_values)} RHR "
            f"(minimum 7 each recommended)"
        )

    rmssd_mean, rmssd_std, rmssd_lower, rmssd_upper = _compute_bands(rmssd_values)
    rhr_mean, rhr_std, rhr_lower, rhr_upper = _compute_bands(rhr_values)

    # State machine
    rmssd_below = rmssd < rmssd_lower
    rmssd_above = rmssd > rmssd_upper
    rhr_above = resting_hr > rhr_upper
    rhr_below = resting_hr < rhr_lower

    if rmssd_below and rhr_above:
        state = ReadinessState.SYMPATHETIC_STRESS
        recommendation = (
            "Sympathetic stress detected: HRV below baseline AND RHR above baseline. "
            "Enforce complete rest or strict Zone 1 recovery only."
        )
    elif rmssd_above and rhr_below:
        state = ReadinessState.PARASYMPATHETIC_HYPERACTIVITY
        recommendation = (
            "Parasympathetic hyperactivity: HRV abnormally high AND RHR abnormally low. "
            "Indicates deep systemic exhaustion. Cap intensity; permit steady endurance only."
        )
    else:
        state = ReadinessState.COPING
        recommendation = (
            "Coping well: HRV and RHR within normal bands. "
            "Proceed with planned training intensity."
        )

    return ReadinessResult(
        date=target_date,
        rmssd=rmssd,
        resting_hr=resting_hr,
        rmssd_mean=rmssd_mean,
        rmssd_std=rmssd_std,
        rhr_mean=rhr_mean,
        rhr_std=rhr_std,
        rmssd_lower_band=rmssd_lower,
        rmssd_upper_band=rmssd_upper,
        rhr_lower_band=rhr_lower,
        rhr_upper_band=rhr_upper,
        state=state,
        recommendation=recommendation,
        confidence="high" if len(rmssd_values) >= 7 and len(rhr_values) >= 7 else "low",
    )


def assess_all_dates(
    wellness_records: list[dict[str, Any]],
    window: int = 30,
) -> list[ReadinessResult]:
    """Assess readiness for every date in the wellness records."""
    results = []
    dates = sorted(set(r["id"] for r in wellness_records))
    for date in dates:
        try:
            result = assess_readiness(wellness_records, target_date=date, window=window)
            results.append(result)
        except ValueError as e:
            logger.warning(f"Skipping {date}: {e}")
    return results


def readiness_to_dict(result: ReadinessResult) -> dict[str, Any]:
    """Serialize ReadinessResult to a plain dict for JSON/prompt injection."""
    return {
        "date": result.date,
        "rmssd": result.rmssd,
        "resting_hr": result.resting_hr,
        "rmssd_mean": round(result.rmssd_mean, 2),
        "rmssd_std": round(result.rmssd_std, 2),
        "rhr_mean": round(result.rhr_mean, 2),
        "rhr_std": round(result.rhr_std, 2),
        "rmssd_band": [round(result.rmssd_lower_band, 2), round(result.rmssd_upper_band, 2)],
        "rhr_band": [round(result.rhr_lower_band, 2), round(result.rhr_upper_band, 2)],
        "state": result.state.value,
        "recommendation": result.recommendation,
        "confidence": result.confidence,
    }