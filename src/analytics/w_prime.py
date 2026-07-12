"""
Dynamic FRC (W') Tracking.

Models the kilojoule drawdown and reconstitution of Functional Reserve Capacity
during high-intensity efforts using the W'BAL-ODE model.

Progression trigger: if minimum W' balance during a sprint session stays above 40%,
increase wattage or rep count for the next session.

Sources:
- W'BAL-ODE: Skiba & Clarke (2021) Int J Sports Physiol Perform 16(11):1561-1572
- Adaptive tau: Skiba & Clarke (2021) tau = 546*exp(-0.01*D_CP) + 316
- Original W'bal-INT: Skiba & Jones (2012) Eur J Appl Physiol 112(11):3803-3812
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from .power_metrics import _compute_normalized_power

logger = logging.getLogger(__name__)

# Skiba & Clarke 2021: adaptive tau based on recovery intensity
# tau = 546 * exp(-0.01 * D_CP) + 316, where D_CP = CP - recovery_power
# At CP (D_CP=0): tau ≈ 862s. At 200W below CP: tau ≈ 316s.
def _compute_tau(cp_estimate: float, current_power: float) -> float:
    """Compute adaptive W' recovery time constant (Skiba & Clarke 2021)."""
    d_cp = max(cp_estimate - current_power, 0.0)
    return 546.0 * np.exp(-0.01 * d_cp) + 316.0


@dataclass
class WPrimeResult:
    """W' analysis for a single activity."""

    activity_id: str
    w_prime_capacity: float  # estimated W' in kJ
    min_balance_pct: float  # lowest W' balance as % of capacity
    final_balance_pct: float  # W' balance at end of activity as %
    progression_recommended: bool  # True if min_balance > 40%
    balance_samples: list[tuple[float, float]]  # (elapsed, balance_kj)


def estimate_w_prime_from_activity(
    activity_id: str,
    power_samples: list[float],
    cp_estimate: float | None = None,
    w_prime_capacity: float | None = None,
    tau: float | None = None,
    min_balance_threshold: float = 0.40,
) -> WPrimeResult:
    """
    Track W' balance over the course of an activity.

    Uses the W'BAL-ODE model (Skiba & Clarke 2021):
      dW'/dt = -excess_power + (W'_max - W') / tau
    where tau is adaptive based on recovery intensity:
      tau = 546 * exp(-0.01 * (CP - power)) + 316

    Args:
        activity_id: Unique identifier for the activity.
        power_samples: Power in watts at each second.
        cp_estimate: Critical power estimate in watts. If None, uses NP.
        w_prime_capacity: Estimated W' capacity in kJ. If None, estimated from data.
        tau: Deprecated. If provided, used as fixed tau (for backward compatibility).
             If None (default), uses adaptive tau from Skiba & Clarke 2021.
        min_balance_threshold: Min balance % to trigger progression.

    Returns:
        WPrimeResult with balance tracking and progression recommendation.
    """
    if not power_samples:
        return WPrimeResult(
            activity_id=activity_id,
            w_prime_capacity=0.0,
            min_balance_pct=0.0,
            final_balance_pct=0.0,
            progression_recommended=False,
            balance_samples=[],
        )

    power = np.array(power_samples)
    duration = len(power)
    if cp_estimate is None:
        cp_estimate = _compute_normalized_power(power)

    # Estimate W' capacity if not provided (max excess power integral over short bursts)
    if w_prime_capacity is None:
        excess = np.maximum(power - cp_estimate, 0)
        # Use peak 30s excess as rough W' estimate
        if len(excess) >= 30:
            rolling_30s = np.convolve(excess, np.ones(30) / 30, mode="valid")
            w_prime_capacity = float(np.max(rolling_30s)) * 30 / 1000.0  # to kJ
        else:
            w_prime_capacity = float(np.sum(excess)) / 1000.0

    if w_prime_capacity <= 0:
        return WPrimeResult(
            activity_id=activity_id,
            w_prime_capacity=0.0,
            min_balance_pct=100.0,
            final_balance_pct=100.0,
            progression_recommended=False,
            balance_samples=[],
        )

    # Track W' balance over time
    balance = w_prime_capacity  # start full
    balance_samples = [(0.0, balance)]
    min_balance = w_prime_capacity  # track true minimum across ALL iterations

    # Use adaptive tau (Skiba & Clarke 2021) unless explicitly overridden
    adaptive = tau is None

    for i, p in enumerate(power):
        excess = max(p - cp_estimate, 0.0)  # watts above CP
        drawdown = excess / 1000.0  # convert to kJ per second

        # Recovery: exponential reconstitution with adaptive or fixed tau
        if adaptive:
            current_tau = _compute_tau(cp_estimate, p)
        else:
            current_tau = tau
        recovery = (w_prime_capacity - balance) / current_tau

        balance = balance - drawdown + recovery
        balance = max(0.0, min(balance, w_prime_capacity))

        if balance < min_balance:
            min_balance = balance

        if i % 10 == 0:  # sample every 10 seconds for storage
            balance_samples.append((float(i), balance))
    final_balance = balance  # use actual last balance from the loop

    min_balance_pct = min_balance / w_prime_capacity if w_prime_capacity > 0 else 1.0
    final_balance_pct = final_balance / w_prime_capacity if w_prime_capacity > 0 else 1.0

    progression = min_balance_pct > min_balance_threshold

    logger.info(
        f"W' analysis for {activity_id}: capacity={w_prime_capacity:.1f} kJ, "
        f"min_balance={min_balance_pct:.1%}, progression={progression}"
    )

    return WPrimeResult(
        activity_id=activity_id,
        w_prime_capacity=round(w_prime_capacity, 2),
        min_balance_pct=round(min_balance_pct, 4),
        final_balance_pct=round(final_balance_pct, 4),
        progression_recommended=progression,
        balance_samples=balance_samples,
    )


def w_prime_to_dict(result: WPrimeResult) -> dict[str, Any]:
    """Serialize WPrimeResult to a plain dict (excludes balance_samples for brevity)."""
    return {
        "activity_id": result.activity_id,
        "w_prime_capacity": result.w_prime_capacity,
        "min_balance_pct": result.min_balance_pct,
        "final_balance_pct": result.final_balance_pct,
        "progression_recommended": result.progression_recommended,
    }