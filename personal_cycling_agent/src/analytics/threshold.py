"""
AlphaHRV (DFA-a1) Threshold Modeler.

Analyzes fractal heart rate correlation (DFA-a1) against power to map
metabolic thresholds without formal testing:
- LT1 (Aerobic Threshold): DFA-a1 intersects 0.75
- LT2 (Critical Power): DFA-a1 intersects 0.50
- Zone 2 Audit: flags rides where DFA-a1 < 0.75 for >10% of duration.
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np
try:
    from src.config.constants import (
        DFA_LT1_TARGET,
        DFA_LT2_TARGET,
        DFA_ZONE2_AUDIT_PASS_THRESHOLD,
        DFA_ZONE2_VIOLATION_THRESHOLD,
    )
except ImportError:
    from ..config.constants import (
        DFA_LT1_TARGET,
        DFA_LT2_TARGET,
        DFA_ZONE2_AUDIT_PASS_THRESHOLD,
        DFA_ZONE2_VIOLATION_THRESHOLD,
    )

logger = logging.getLogger(__name__)


@dataclass
class ThresholdResult:
    """DFA-a1 threshold analysis for a single activity."""

    activity_id: str
    lt1_power: float | None  # power at DFA-a1 = 0.75
    lt2_power: float | None  # power at DFA-a1 = 0.50
    zone2_violation_pct: float  # % of ride where DFA-a1 < 0.75
    zone2_audit_passed: bool  # True if violation < 10%


def _interpolate_power_at_dfa(
    power_values: list[float],
    dfa_values: list[float],
    target_dfa: float,
) -> float | None:
    """
    Find the power output where DFA-a1 crosses a target value.

    Collects all crossings and returns the mean interpolated power.
    Returns None if the target is never crossed.
    """
    if len(power_values) != len(dfa_values):
        raise ValueError("Power and DFA arrays must have the same length")

    crossings: list[float] = []

    for i in range(len(dfa_values) - 1):
        d1 = dfa_values[i]
        d2 = dfa_values[i + 1]

        # Check if target is bracketed
        if (d1 >= target_dfa and d2 <= target_dfa) or (d1 <= target_dfa and d2 >= target_dfa):
            if d1 == d2:
                continue
            # Linear interpolation
            t = (target_dfa - d1) / (d2 - d1)
            crossings.append(float(power_values[i] + t * (power_values[i + 1] - power_values[i])))

    if not crossings:
        return None

    return sum(crossings) / len(crossings)


def analyze_thresholds(
    activity_id: str,
    power_samples: list[float],
    dfa_samples: list[float],
    lt1_target: float = DFA_LT1_TARGET,
    lt2_target: float = DFA_LT2_TARGET,
    zone2_violation_threshold: float = DFA_ZONE2_AUDIT_PASS_THRESHOLD,
) -> ThresholdResult:
    """
    Analyze DFA-a1 vs power to find metabolic thresholds.
    Args:
        activity_id: Intervals.icu activity ID for logging.
        power_samples: Power values in watts (aligned with dfa_samples).
        dfa_samples: DFA-a1 values from AlphaHRV (aligned with power_samples).
        lt1_target: DFA-a1 value for aerobic threshold (default 0.75).
        lt2_target: DFA-a1 value for critical power (default 0.50).
        zone2_violation_threshold: Max % of ride below LT1 before flagging.

    Returns:
        ThresholdResult with LT1, LT2, and Zone 2 audit status.
    """
    if not power_samples or not dfa_samples:
        logger.warning(f"No data for threshold analysis on {activity_id}")
        return ThresholdResult(
            activity_id=activity_id,
            lt1_power=None,
            lt2_power=None,
            zone2_violation_pct=0.0,
            zone2_audit_passed=True,
        )

    if len(power_samples) != len(dfa_samples):
        raise ValueError(
            f"Power ({len(power_samples)}) and DFA ({len(dfa_samples)}) "
            f"arrays must match for {activity_id}"
        )

    # Find thresholds
    lt1_power = _interpolate_power_at_dfa(power_samples, dfa_samples, lt1_target)
    lt2_power = _interpolate_power_at_dfa(power_samples, dfa_samples, lt2_target)

    # Zone 2 audit: what % of the ride has DFA-a1 below LT1 threshold?
    below_lt1 = sum(1 for d in dfa_samples if d < lt1_target)
    violation_pct = below_lt1 / len(dfa_samples) if dfa_samples else 0.0

    audit_passed = violation_pct <= zone2_violation_threshold

    logger.info(
        f"Threshold analysis for {activity_id}: "
        f"LT1={lt1_power}, LT2={lt2_power}, "
        f"Zone2 violation={violation_pct:.1%}, passed={audit_passed}"
    )

    return ThresholdResult(
        activity_id=activity_id,
        lt1_power=lt1_power,
        lt2_power=lt2_power,
        zone2_violation_pct=round(violation_pct, 4),
        zone2_audit_passed=audit_passed,
    )


def analyze_batch(
    activity_data: list[dict[str, Any]],
) -> list[ThresholdResult]:
    """
    Run threshold analysis on multiple activities.

    Each dict must have: 'activity_id', 'power', 'dfa_a1'.
    """
    results = []
    for data in activity_data:
        aid = data.get("activity_id", "unknown")
        power = data.get("power", [])
        dfa = data.get("dfa_a1", [])
        try:
            result = analyze_thresholds(aid, power, dfa)
            results.append(result)
        except ValueError as e:
            logger.warning(f"Threshold analysis failed for {aid}: {e}")
    return results


def threshold_to_dict(result: ThresholdResult) -> dict[str, Any]:
    """Serialize ThresholdResult to a plain dict."""
    return {
        "activity_id": result.activity_id,
        "lt1_power": result.lt1_power,
        "lt2_power": result.lt2_power,
        "zone2_violation_pct": result.zone2_violation_pct,
        "zone2_audit_passed": result.zone2_audit_passed,
    }