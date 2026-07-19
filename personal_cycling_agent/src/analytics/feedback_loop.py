"""
Post-ride feedback loop for plan mutation.

After a ride is completed, compares actual outcomes against the planned
prescription and mutates the next day's plan accordingly.

Based on:
- Rothschild et al. 2024 (Eur J Appl Physiol): Post-ride feedback loop
- Domestique (MIT license): Plan mutation from outcomes
"""

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FeedbackResult:
    """Result of post-ride feedback analysis."""
    plan_mutated: bool
    mutation_type: str  # reduce_intensity, increase_volume, maintain, rest_day
    next_day_tss_adjustment: float  # multiplier for next day's TSS target
    next_day_zone_shift: int  # -1 = lower zones, 0 = no shift, +1 = higher zones
    reason: str


def analyze_post_ride_feedback(
    planned_tss: float,
    actual_tss: float,
    planned_zones: dict[str, float],  # zone name -> planned percentage
    actual_zones: dict[str, float],  # zone name -> actual percentage
    planned_intensity: float,  # 0-1 scale
    actual_intensity: float,  # 0-1 scale
    decoupling_drift: float | None = None,
    w_prime_balance: float | None = None,
    ftp_drift: float | None = None,
) -> FeedbackResult:
    """
    Analyze post-ride outcomes and suggest plan mutations.

    Logic (from research):
    - TSS overshoot → reduce next day
    - Decoupling increase → more aerobic base
    - eFTP drift up → increase load
    - Zone mismatch → adjust zone targets

    Args:
        planned_tss: Planned Training Stress Score.
        actual_tss: Actual TSS from completed ride.
        planned_zones: Planned time in each zone (percentage).
        actual_zones: Actual time in each zone (percentage).
        planned_intensity: Planned intensity (0-1, where 1 = max effort).
        actual_intensity: Actual intensity achieved (0-1).
        decoupling_drift: Power:HR drift percentage (negative = good, positive = bad).
        w_prime_balance: W' balance percentage (0 = depleted, 100 = full).
        ftp_drift: FTP change since last estimate (positive = improvement).

    Returns:
        FeedbackResult with mutation recommendations.
    """
    # --- TSS Analysis ---
    tss_ratio = actual_tss / planned_tss if planned_tss > 0 else 1.0

    # --- Zone Analysis ---
    # Zone drift: positive = went harder than planned, negative = went easier
    high_zone_actual = sum(actual_zones.get(z, 0) for z in ["Z4", "Z5"])
    high_zone_planned = sum(planned_zones.get(z, 0) for z in ["Z4", "Z5"])
    zone_drift = high_zone_actual - high_zone_planned

    # --- Intensity Analysis ---
    intensity_ratio = actual_intensity / planned_intensity if planned_intensity > 0 else 1.0

    # --- Decision Logic ---
    tss_adjustment = 1.0
    zone_shift = 0
    reasons = []

    # Rule 1: TSS overshoot → reduce next day
    if tss_ratio > 1.3:
        tss_adjustment = max(0.6, 1.0 - (tss_ratio - 1.0) * 0.5)
        reasons.append(f"TSS overshoot ({tss_ratio:.0%} of plan) — reduce next day")
    elif tss_ratio < 0.7:
        tss_adjustment = min(1.3, 1.0 + (1.0 - tss_ratio) * 0.3)
        reasons.append(f"TSS undershoot ({tss_ratio:.0%} of plan) — can increase next day")

    # Rule 2: Decoupling increase → more aerobic base
    if decoupling_drift is not None and decoupling_drift > 5.0:
        zone_shift = -1  # Shift to lower zones (more aerobic)
        reasons.append(f"Decoupling drift ({decoupling_drift:.1f}%) — add aerobic base work")

    # Rule 3: FTP drift up → increase load
    if ftp_drift is not None and ftp_drift > 0:
        tss_adjustment = min(1.3, tss_adjustment + ftp_drift * 0.01)
        reasons.append(f"FTP improved ({ftp_drift:.0f}W) — can increase load")

    # Rule 4: W' depletion → recovery focus
    if w_prime_balance is not None and w_prime_balance < 20.0:
        tss_adjustment = max(0.5, tss_adjustment * 0.7)
        zone_shift = min(zone_shift, -1)
        reasons.append(f"W' depleted ({w_prime_balance:.0f}%) — prioritize recovery")

    # Rule 5: Zone mismatch → adjust targets
    if abs(zone_drift) > 20.0:
        # zone_drift > 0 means rider went harder than planned → shift lower
        zone_shift = min(zone_shift, -1) if zone_drift > 0 else max(zone_shift, 1)
        reasons.append(f"Zone mismatch ({zone_drift:+.0f}%) — adjust zone targets")

    # --- Determine mutation type ---
    if tss_adjustment < 0.7:
        mutation_type = "rest_day"
    elif tss_adjustment < 0.9:
        mutation_type = "reduce_intensity"
    elif tss_adjustment > 1.1:
        mutation_type = "increase_volume"
    else:
        mutation_type = "maintain"

    plan_mutated = mutation_type != "maintain"
    reason = "; ".join(reasons) if reasons else "No significant deviations — maintain plan"

    return FeedbackResult(
        plan_mutated=plan_mutated,
        mutation_type=mutation_type,
        next_day_tss_adjustment=round(tss_adjustment, 2),
        next_day_zone_shift=zone_shift,
        reason=reason,
    )


def feedback_to_dict(result: FeedbackResult) -> dict[str, Any]:
    """Serialize FeedbackResult to a plain dict."""
    return {
        "plan_mutated": result.plan_mutated,
        "mutation_type": result.mutation_type,
        "next_day_tss_adjustment": result.next_day_tss_adjustment,
        "next_day_zone_shift": result.next_day_zone_shift,
        "reason": result.reason,
    }