"""
Multi-Modal Readiness Engine.

Combines autonomic state (HRV, RHR), daily stress, and recent training load
into a composite readiness score with Kiviniemi-style decision logic for
load modulation.

Based on:
- Alfonso et al. 2025 (Sci Rep): HRV + RHR + subjective WB → 2.5x greater FTP gains
- Kiviniemi et al. 2007 (Eur J Appl Physiol): HRV-guided training protocol
- Rothschild et al. 2024 (Eur J Appl Physiol): Multi-modal ML recovery model
- Saw et al. 2016: Readiness index weighting
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class ReadinessState(Enum):
    """Readiness states following Kiviniemi/Plews classification."""
    OPTIMAL = "optimal"
    COPING = "coping"
    SYMPATHETIC_STRESS = "sympathetic_stress"
    PARASYMPATHETIC_HYPERACTIVITY = "parasympathetic_hyperactivity"
    EXHAUSTED = "exhausted"


@dataclass
class ReadinessResult:
    """Output of the readiness analysis for a single day."""
    date: str
    state: ReadinessState
    recommendation: str
    confidence: str

    # Composite score (0-100, higher = more ready)
    composite_score: float

    # Sub-scores (0-100 scale)
    autonomic_score: float
    stress_score: float
    load_score: float

    # Raw metrics
    rmssd: float | None
    resting_hr: float | None
    stress: float | None
    recent_tss: float | None

    # Baseline stats
    rmssd_mean: float
    rmssd_std: float
    rhr_mean: float
    rhr_std: float
    stress_mean: float
    stress_std: float

    # Kiviniemi normality bands (mean ± 0.5*SD per protocol)
    rmssd_lower_band: float
    rmssd_upper_band: float
    rhr_lower_band: float
    rhr_upper_band: float

    # Load modulation factor (0.0-1.0, multiply planned TSS by this)
    load_modulation: float

    # Limiting factor (which metric is dragging readiness down)
    limiting_factor: str


def _compute_bands(
    values: list[float], window: int = 30
) -> tuple[float, float, float, float]:
    """
    Compute rolling mean, std, and Kiviniemi normality bands (mean ± 0.5*SD).

    Returns (mean, std, lower, upper).
    Uses 0.5*SD per Kiviniemi 2007 protocol (not 0.75*SD).
    """
    arr = np.array(values)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
    band = 0.5 * std  # Kiviniemi protocol
    lower = mean - band
    upper = mean + band
    return mean, std, lower, upper


def _zscore(value: float | None, mean: float, std: float) -> float | None:
    """Compute z-score, returning None if value is None or std is 0."""
    if value is None or std == 0:
        return None
    return (value - mean) / std


def _autonomic_score(
    rmssd: float | None,
    rhr: float | None,
    rmssd_mean: float,
    rmssd_std: float,
    rhr_mean: float,
    rhr_std: float,
) -> tuple[float, str]:
    """
    Compute autonomic readiness score (0-100).

    Higher RMSSD and lower RHR indicate better autonomic state.
    Score is based on z-score deviation from baseline.

    Returns (score, limiting_factor).
    """
    rmssd_z = _zscore(rmssd, rmssd_mean, rmssd_std)
    rhr_z = _zscore(rhr, rhr_mean, rhr_std)

    # Start at 50 (neutral), adjust based on z-scores
    score = 50.0
    limiting = "none"

    if rmssd_z is not None:
        # Positive RMSSD z-score = better than baseline → add points
        score += rmssd_z * 15  # ±15 points per SD
        if rmssd_z < -0.5:
            limiting = "rmssd_low"

    if rhr_z is not None:
        # Negative RHR z-score = lower than baseline → better (subtract z-score)
        score -= rhr_z * 15  # ±15 points per SD
        if rhr_z > 0.5:
            limiting = "rhr_high"

    # If neither metric available, return neutral
    if rmssd is None and rhr is None:
        return 50.0, "no_autonomic_data"

    return float(np.clip(score, 0, 100)), limiting


def _stress_score(
    stress: float | None,
    stress_mean: float,
    stress_std: float,
) -> tuple[float, str]:
    """
    Compute stress readiness score (0-100).

    Lower stress = better readiness. Garmin stress is 0-100.
    """
    if stress is None:
        return 50.0, "no_stress_data"

    stress_z = _zscore(stress, stress_mean, stress_std)

    score = 50.0
    if stress_z is not None:
        # Negative z-score = lower than baseline → better
        score -= stress_z * 20  # ±20 points per SD (stress is a strong predictor)
        if stress_z > 0.5:
            return float(np.clip(score, 0, 100)), "stress_high"

    return float(np.clip(score, 0, 100)), "none"


def _load_score(
    recent_tss: float | None,
    ctl: float | None,
    atl: float | None,
) -> tuple[float, str]:
    """
    Compute training load readiness score (0-100).

    Based on ACWR (Acute:Chronic Workload Ratio).
    Optimal ACWR is 0.8-1.3 (Gabbett 2016).
    """
    if recent_tss is None and atl is None:
        return 50.0, "no_load_data"

    if atl is not None and ctl is not None and ctl > 0:
        acwr = atl / ctl
    elif recent_tss is not None and ctl is not None and ctl > 0:
        acwr = recent_tss / ctl
    else:
        return 50.0, "insufficient_load_data"

    # ACWR sweet spot: 0.8-1.3
    if 0.8 <= acwr <= 1.3:
        score = 80.0
        limiting = "none"
    elif acwr < 0.8:
        # Undertraining — still ready, just not peaking
        score = 60.0 + (acwr / 0.8) * 20
        limiting = "undertraining"
    elif acwr > 1.3 and acwr <= 1.5:
        # Danger zone — some fatigue
        score = 80.0 - ((acwr - 1.3) / 0.2) * 30
        limiting = "overreaching"
    else:
        # Injury risk zone
        score = max(20.0, 50.0 - (acwr - 1.5) * 25)
        limiting = "injury_risk"

    return float(np.clip(score, 0, 100)), limiting


def _kiviniemi_decision(
    rmssd: float | None,
    rhr: float | None,
    rmssd_mean: float,
    rmssd_std: float,
    rhr_mean: float,
    rhr_std: float,
    rmssd_lower: float,
    rmssd_upper: float,
    rhr_lower: float,
    rhr_upper: float,
) -> tuple[ReadinessState, str, float]:
    """
    Kiviniemi-style decision logic for training prescription.

    Returns (state, recommendation, load_modulation).

    Protocol (Kiviniemi 2007):
    - Within normality range → prescribe as planned (modulation=1.0)
    - 0.5*SD below normal → reduce intensity 20-30% (modulation=0.7-0.8)
    - >1*SD below normal → rest or active recovery (modulation=0.3-0.5)
    """
    rmssd_below_normal = rmssd is not None and rmssd < rmssd_lower
    rmssd_above_normal = rmssd is not None and rmssd > rmssd_upper
    rhr_above_normal = rhr is not None and rhr > rhr_upper
    rhr_below_normal = rhr is not None and rhr < rhr_lower

    # Compute deviation magnitude (how many SDs from mean)
    rmssd_dev = 0.0
    if rmssd is not None and rmssd_std > 0:
        rmssd_dev = (rmssd - rmssd_mean) / rmssd_std

    rhr_dev = 0.0
    if rhr is not None and rhr_std > 0:
        rhr_dev = (rhr - rhr_mean) / rhr_std

    # Sympathetic stress: HRV low AND/OR RHR high
    if rmssd_below_normal and rhr_above_normal:
        # Both signals agree — strong sympathetic stress
        severity = min(abs(rmssd_dev) + abs(rhr_dev), 4.0) / 4.0
        modulation = max(0.3, 1.0 - severity * 0.7)
        return (
            ReadinessState.SYMPATHETIC_STRESS,
            "Sympathetic stress: HRV below AND RHR above baseline. "
            "Reduce intensity significantly or take rest day.",
            modulation,
        )

    # Parasympathetic hyperactivity: HRV high AND/OR RHR low (exhaustion signal)
    if rmssd_above_normal and rhr_below_normal:
        severity = min(abs(rmssd_dev) + abs(rhr_dev), 4.0) / 4.0
        modulation = max(0.4, 1.0 - severity * 0.6)
        return (
            ReadinessState.PARASYMPATHETIC_HYPERACTIVITY,
            "Parasympathetic hyperactivity: possible deep exhaustion. "
            "Cap intensity; steady endurance only.",
            modulation,
        )

    # Single-metric deviations
    if rmssd_below_normal:
        severity = min(abs(rmssd_dev), 3.0) / 3.0
        modulation = max(0.5, 1.0 - severity * 0.5)
        return (
            ReadinessState.SYMPATHETIC_STRESS,
            "HRV below baseline. Consider reducing intensity.",
            modulation,
        )

    if rhr_above_normal:
        severity = min(abs(rhr_dev), 3.0) / 3.0
        modulation = max(0.5, 1.0 - severity * 0.5)
        return (
            ReadinessState.SYMPATHETIC_STRESS,
            "RHR above baseline. Consider reducing intensity.",
            modulation,
        )

    if rmssd_above_normal:
        return (
            ReadinessState.PARASYMPATHETIC_HYPERACTIVITY,
            "HRV above baseline. May indicate exhaustion; cap intensity.",
            0.8,
        )

    if rhr_below_normal:
        return (
            ReadinessState.PARASYMPATHETIC_HYPERACTIVITY,
            "RHR below baseline. May indicate exhaustion.",
            0.85,
        )

    # Within normal bands
    if rmssd is not None or rhr is not None:
        # Both metrics in range — optimal
        if rmssd is not None and rhr is not None:
            return (
                ReadinessState.OPTIMAL,
                "All metrics within normal bands. Proceed with planned training.",
                1.0,
            )
        else:
            return (
                ReadinessState.COPING,
                "Available metrics within normal bands. Proceed with planned training.",
                0.95,
            )

    # No autonomic data
    return (
        ReadinessState.COPING,
        "No autonomic data available. Proceed with caution.",
        0.8,
    )


def assess_readiness(
    wellness_records: list[dict[str, Any]],
    activity_metrics: list[dict[str, Any]] | None = None,
    target_date: str | None = None,
    window: int = 30,
) -> ReadinessResult:
    """
    Multi-modal readiness assessment.

    Combines:
    - Autonomic state (HRV + RHR) — 40% weight
    - Daily stress — 25% weight
    - Training load (ACWR) — 35% weight

    Args:
        wellness_records: List of wellness dicts with 'date', 'rmssd', 'resting_hr',
            'stress', 'sleep_score', 'sleep_hours', 'body_battery_end'.
        activity_metrics: List of activity dicts with 'start_date', 'tss'.
        target_date: ISO date string to assess. Defaults to most recent.
        window: Number of days for rolling baseline.

    Returns:
        ReadinessResult with composite score and recommendation.
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
    stress = today.get("stress")

    # Build baseline (N days before today, not including today)
    cutoff = date.fromisoformat(target_date) - timedelta(days=window)
    baseline_records = [
        r
        for r in records
        if cutoff <= date.fromisoformat(r.get("date", "")) < date.fromisoformat(target_date)
    ]

    # Extract baseline values
    rmssd_values = [r["rmssd"] for r in baseline_records if r.get("rmssd") is not None]
    rhr_values = [r["resting_hr"] for r in baseline_records if r.get("resting_hr") is not None]
    stress_values = [r["stress"] for r in baseline_records if r.get("stress") is not None]

    # Compute bands
    if rmssd_values:
        rmssd_mean, rmssd_std, rmssd_lower, rmssd_upper = _compute_bands(rmssd_values)
    else:
        rmssd_mean, rmssd_std, rmssd_lower, rmssd_upper = 0.0, 0.0, 0.0, 0.0

    if rhr_values:
        rhr_mean, rhr_std, rhr_lower, rhr_upper = _compute_bands(rhr_values)
    else:
        rhr_mean, rhr_std, rhr_lower, rhr_upper = 0.0, 0.0, 0.0, 0.0

    if stress_values:
        stress_mean, stress_std, _, _ = _compute_bands(stress_values)
    else:
        stress_mean, stress_std = 0.0, 0.0

    # Compute sub-scores
    autonomic_score, auto_limiting = _autonomic_score(
        rmssd, resting_hr, rmssd_mean, rmssd_std, rhr_mean, rhr_std
    )
    stress_score, stress_limiting = _stress_score(stress, stress_mean, stress_std)

    # Compute recent load metrics
    recent_tss = None
    ctl = None
    atl = None

    if activity_metrics:
        act_cutoff = date.fromisoformat(target_date) - timedelta(days=30)
        recent_activities = []
        for a in activity_metrics:
            sd = a.get("start_date", "")
            if not sd:
                continue
            try:
                act_date = date.fromisoformat(sd)
            except ValueError:
                continue
            if act_date >= act_cutoff:
                recent_activities.append(a)

        # 7-day TSS (acute)
        week_ago = date.fromisoformat(target_date) - timedelta(days=7)
        week_tss = 0.0
        daily_tss = {}
        for a in recent_activities:
            sd = a.get("start_date", "")
            if not sd:
                continue
            try:
                act_date = date.fromisoformat(sd)
            except ValueError:
                continue
            tss_val = a.get("tss", 0) or 0
            daily_tss[act_date] = daily_tss.get(act_date, 0) + tss_val
            if act_date >= week_ago:
                week_tss += tss_val
        if week_tss > 0:
            recent_tss = week_tss

            # Build time series for EWMA
            all_dates = list(range(
                (cutoff - timedelta(days=1)).toordinal(),
                date.fromisoformat(target_date).toordinal() + 1
            ))
            tss_series = []
            for ord_ in all_dates:
                d = date.fromordinal(ord_)
                tss_series.append(daily_tss.get(d, 0))

            if tss_series:
                tss_arr = np.array(tss_series, dtype=float)
                # CTL: 18-day half-life
                ctl_alpha = 1 - np.exp(np.log(0.5) / 18)
                ctl = float(pd.Series(tss_arr).ewm(alpha=ctl_alpha, min_periods=1).mean().iloc[-1])
                # ATL: 7-day half-life
                atl_alpha = 1 - np.exp(np.log(0.5) / 7)
                atl = float(pd.Series(tss_arr).ewm(alpha=atl_alpha, min_periods=1).mean().iloc[-1])

    load_score, load_limiting = _load_score(recent_tss, ctl, atl)

    # Kiviniemi decision logic
    state, kiviniemi_rec, load_modulation = _kiviniemi_decision(
        rmssd, resting_hr,
        rmssd_mean, rmssd_std, rhr_mean, rhr_std,
        rmssd_lower, rmssd_upper, rhr_lower, rhr_upper,
    )

    # Composite score (weighted average per Saw 2016 / Alfonso 2025)
    # Autonomic: 40%, Stress: 25%, Load: 35%
    composite = (
        autonomic_score * 0.40 +
        stress_score * 0.25 +
        load_score * 0.35
    )
    composite = float(np.clip(composite, 0, 100))

    # Determine limiting factor (lowest sub-score)
    scores = {
        "autonomic": autonomic_score,
        "stress": stress_score,
        "load": load_score,
    }
    limiting = min(scores, key=scores.get)

    # Build recommendation based on state and limiting factor
    if state == ReadinessState.OPTIMAL:
        recommendation = (
            f"Optimal readiness (score: {composite:.0f}/100). "
            f"All systems nominal. Proceed with planned training at full intensity."
        )
    elif state == ReadinessState.COPING:
        recommendation = (
            f"Coping (score: {composite:.0f}/100). "
            f"{kiviniemi_rec} "
            f"Limiting factor: {limiting}."
        )
    elif state == ReadinessState.SYMPATHETIC_STRESS:
        recommendation = (
            f"Sympathetic stress (score: {composite:.0f}/100). "
            f"{kiviniemi_rec} "
            f"Reduce planned TSS by {int((1 - load_modulation) * 100)}%."
        )
    elif state == ReadinessState.PARASYMPATHETIC_HYPERACTIVITY:
        recommendation = (
            f"Possible exhaustion (score: {composite:.0f}/100). "
            f"{kiviniemi_rec}"
        )
    else:
        recommendation = kivinieme_rec

    # Confidence based on data availability
    data_points = sum([
        rmssd is not None,
        resting_hr is not None,
        stress is not None,
        recent_tss is not None,
    ])
    if data_points >= 4 and len(rmssd_values) >= 7 and len(rhr_values) >= 7:
        confidence = "high"
    elif data_points >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return ReadinessResult(
        date=target_date,
        state=state,
        recommendation=recommendation,
        confidence=confidence,
        composite_score=composite,
        autonomic_score=autonomic_score,
        stress_score=stress_score,
        load_score=load_score,
        rmssd=rmssd,
        resting_hr=resting_hr,
        stress=stress,
        recent_tss=recent_tss,
        rmssd_mean=rmssd_mean,
        rmssd_std=rmssd_std,
        rhr_mean=rhr_mean,
        rhr_std=rhr_std,
        stress_mean=stress_mean,
        stress_std=stress_std,
        rmssd_lower_band=rmssd_lower,
        rmssd_upper_band=rmssd_upper,
        rhr_lower_band=rhr_lower,
        rhr_upper_band=rhr_upper,
        load_modulation=load_modulation,
        limiting_factor=limiting,
    )


def assess_all_dates(
    wellness_records: list[dict[str, Any]],
    activity_metrics: list[dict[str, Any]] | None = None,
    window: int = 30,
) -> list[ReadinessResult]:
    """Assess readiness for every date in the wellness records."""
    results = []
    dates = sorted(set(r.get("date", "") for r in wellness_records))
    for d in dates:
        try:
            result = assess_readiness(wellness_records, activity_metrics, d, window)
            results.append(result)
        except (ValueError, KeyError):
            continue
    return results


def readiness_to_dict(result: ReadinessResult) -> dict[str, Any]:
    """Serialize ReadinessResult to a plain dict for JSON/prompt injection."""
    return {
        "date": result.date,
        "state": result.state.value,
        "recommendation": result.recommendation,
        "confidence": result.confidence,
        "composite_score": result.composite_score,
        "autonomic_score": result.autonomic_score,
        "stress_score": result.stress_score,
        "load_score": result.load_score,
        "rmssd": result.rmssd,
        "resting_hr": result.resting_hr,
        "stress": result.stress,
        "recent_tss": result.recent_tss,
        "rmssd_mean": result.rmssd_mean,
        "rmssd_std": result.rmssd_std,
        "rhr_mean": result.rhr_mean,
        "rhr_std": result.rhr_std,
        "stress_mean": result.stress_mean,
        "stress_std": result.stress_std,
        "rmssd_lower_band": result.rmssd_lower_band,
        "rmssd_upper_band": result.rmssd_upper_band,
        "rhr_lower_band": result.rhr_lower_band,
        "rhr_upper_band": result.rhr_upper_band,
        "load_modulation": result.load_modulation,
        "limiting_factor": result.limiting_factor,
    }