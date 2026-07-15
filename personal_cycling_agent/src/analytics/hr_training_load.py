"""
Heart Rate-Based Training Load.

Computes training load from heart rate data using the Banister differential
TRIMP (dTRIMP) formula, normalized to the TSS (Training Stress Score) scale.

Banister TRIMP uses exponential weighting to reflect the non-linear
relationship between heart rate and blood lactate accumulation:
    TRIMP = sum[ (Δ/60) * HRr * 0.64 * exp(b * HRr) ]
where HRr is the fractional heart rate reserve and b is a gender-specific
constant derived from population lactate-HR curves.

TSS normalization divides the session TRIMP by the TRIMP of a hypothetical
60-minute session at threshold intensity (90% HR reserve), scaled to 100.
This places HR-derived load on the same 0-100+ scale as power-based TSS.

An athlete-specific calibration factor (derived from rides with both power
and HR data) corrects for HR lag and individual differences.

Sources:
- Banister TRIMP: Banister et al. (1975), Banister (1991)
- Calibration: Sanders et al. (2017) Int J Sports Physiol Perform
- iTRIMP: Manzi et al. (2009) Med Sci Sports Exerc — iTRIMP defaults to
  Banister TRIMP without individual lactate curves
"""

import logging
import math
from dataclasses import dataclass
from typing import Any

from src.ui_helpers import _HR_RANGES, _zone_for_value

logger = logging.getLogger(__name__)

# Banister dTRIMP gender constants (derived from population lactate-HR curves)
# Male: b=1.92, Female: b=1.67
# Source: Banister, E.W. (1991) Modeling Elite Athletic Performance
_BANISTER_B: dict[str, float] = {"male": 1.92, "female": 1.67}

# Threshold HR fraction of HR reserve for TSS normalization.
# 0.9 = 90% of HR reserve ≈ lactate threshold for most athletes.
# A 60-min session at this intensity should produce ~100 TSS.
_THRESHOLD_HR_FRACTION = 0.9

# Minimum valid HR samples (seconds) to compute a meaningful TRIMP.
_MIN_HR_SAMPLES = 60

# Calibration factor bounds — prevent outlier rides from producing extreme factors.
_CAL_MIN = 0.5
_CAL_MAX = 3.0

# Minimum number of dual-sensor rides for a stable calibration factor.
_MIN_CAL_RIDES = 3


@dataclass
class HrTrainingLoadResult:
    """HR-based training load for a single activity."""
    activity_id: str
    trimp: float  # Raw Banister TRIMP score
    hr_tss: float  # TSS-normalized score (before calibration)
    time_in_hr_zones: dict[str, float]  # zone name -> percentage (0-100)


def _banister_trimp(
    hr_samples: list[float],
    max_hr: float,
    resting_hr: float,
    b: float,
) -> float:
    """
    Compute raw Banister dTRIMP from per-second HR samples.

    TRIMP = sum_i[ (1/60) * HRr_i * 0.64 * exp(b * HRr_i) ]

    Args:
        hr_samples: Per-second heart rate values in bpm.
        max_hr: Maximum heart rate in bpm.
        resting_hr: Resting heart rate in bpm.
        b: Gender-specific Banister constant (1.92 male, 1.67 female).

    Returns:
        Raw TRIMP score. Returns 0.0 if inputs are invalid.
    """
    hr_reserve = max_hr - resting_hr
    if hr_reserve <= 0:
        return 0.0

    trimp = 0.0
    for hr in hr_samples:
        if hr <= 0:
            continue
        hr_r = (hr - resting_hr) / hr_reserve
        hr_r = max(0.0, min(1.0, hr_r))  # clip to [0, 1]
        if hr_r > 0:
            trimp += (1.0 / 60.0) * hr_r * 0.64 * math.exp(b * hr_r)

    return round(trimp, 2)


def _trimp_at_threshold_1hr(
    b: float,
    threshold_hr_fraction: float = _THRESHOLD_HR_FRACTION,
) -> float:
    """
    Compute TRIMP for a hypothetical 60-minute session at threshold intensity.

    For a constant-intensity session, TRIMP simplifies to:
        TRIMP = duration_min * HRr * 0.64 * exp(b * HRr)

    Args:
        b: Gender-specific Banister constant.
        threshold_hr_fraction: HRr at threshold (default 0.9).

    Returns:
        TRIMP score for 60 minutes at threshold.
    """
    hr_r = threshold_hr_fraction
    return 60.0 * hr_r * 0.64 * math.exp(b * hr_r)


def _compute_hr_time_in_zones(
    hr_samples: list[float],
    max_hr: float,
) -> dict[str, float]:
    """
    Compute percentage of time spent in each HR zone.

    Uses the existing _HR_RANGES from ui_helpers.py:
        Z1: <58% Max HR, Z2: 59-74%, Z3: 75-89%, Z4: 90-94%, Z5: >95%

    Returns:
        Dict mapping zone name (Z1-Z5) to percentage (0-100).
    """
    zone_names = ["Z1", "Z2", "Z3", "Z4", "Z5"]
    zone_counts = {name: 0 for name in zone_names}
    total = 0

    for hr in hr_samples:
        if hr <= 0:
            continue
        total += 1
        zone_idx = _zone_for_value(hr, max_hr, _HR_RANGES)
        if 0 <= zone_idx < len(zone_names):
            zone_counts[zone_names[zone_idx]] += 1

    if total == 0:
        return zone_counts

    return {name: (count / total) * 100 for name, count in zone_counts.items()}


def compute_hr_training_load(
    activity_id: str,
    hr_samples: list[float],
    max_hr: float,
    resting_hr: float,
    gender: str = "male",
) -> HrTrainingLoadResult:
    """
    Compute HR-based training load for an activity.

    Args:
        activity_id: Unique identifier for the activity.
        hr_samples: Per-second heart rate values in bpm.
        max_hr: Maximum heart rate in bpm.
        resting_hr: Resting heart rate in bpm.
        gender: "male" or "female" (controls Banister constant b).

    Returns:
        HrTrainingLoadResult with raw TRIMP, TSS-normalized score,
        and time-in-HR-zones. Returns zero scores with a warning log
        if inputs are invalid.
    """
    b = _BANISTER_B.get(gender.lower(), _BANISTER_B["male"])

    # Filter valid samples
    valid_samples = [hr for hr in hr_samples if hr > 0]

    if len(valid_samples) < _MIN_HR_SAMPLES or max_hr <= 0 or max_hr <= resting_hr:
        result = HrTrainingLoadResult(
            activity_id=activity_id,
            trimp=0.0,
            hr_tss=0.0,
            time_in_hr_zones={f"Z{i+1}": 0.0 for i in range(5)},
        )
        logger.warning(
            f"Insufficient HR data for {activity_id}: "
            f"{len(valid_samples)} valid samples, max_hr={max_hr}, resting_hr={resting_hr}"
        )
        return result

    # Compute raw TRIMP
    trimp = _banister_trimp(valid_samples, max_hr, resting_hr, b)

    # Normalize to TSS scale
    trimp_threshold = _trimp_at_threshold_1hr(b)
    hr_tss = (trimp / trimp_threshold) * 100.0 if trimp_threshold > 0 else 0.0

    # Compute time in HR zones
    zones = _compute_hr_time_in_zones(valid_samples, max_hr)

    logger.info(
        f"HR training load for {activity_id}: TRIMP={trimp:.1f}, "
        f"hrTSS={hr_tss:.1f}"
    )

    return HrTrainingLoadResult(
        activity_id=activity_id,
        trimp=round(trimp, 2),
        hr_tss=round(hr_tss, 2),
        time_in_hr_zones={zone: round(pct, 2) for zone, pct in zones.items()},
    )


def compute_hr_tss_with_calibration(
    hr_tss_approx: float,
    calibration_factor: float | None,
) -> float:
    """
    Apply athlete-specific calibration factor to HR-derived TSS.

    Args:
        hr_tss_approx: TSS-normalized HR score (before calibration).
        calibration_factor: k = mean(TSS_power / TSS_hr) from dual-sensor rides.
            If None, returns hr_tss_approx unchanged.

    Returns:
        Calibrated TSS value.
    """
    if calibration_factor is None:
        return hr_tss_approx
    return round(hr_tss_approx * calibration_factor, 2)


def hr_training_load_to_dict(result: HrTrainingLoadResult) -> dict[str, Any]:
    """Serialize HrTrainingLoadResult to a plain dict."""
    return {
        "activity_id": result.activity_id,
        "trimp": result.trimp,
        "hr_tss": result.hr_tss,
        "time_in_hr_zones": result.time_in_hr_zones,
    }