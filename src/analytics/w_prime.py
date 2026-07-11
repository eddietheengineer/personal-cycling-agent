"""
Dynamic FRC (W') Tracking.

Models the kilojoule drawdown and reconstitution of Functional Reserve Capacity
during high-intensity efforts.

Progression trigger: if minimum W' balance during a sprint session stays above 40%,
increase wattage or rep count for the next session.
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

from .power_metrics import _compute_normalized_power

logger = logging.getLogger(__name__)


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
    tau: float = 240.0,
    min_balance_threshold: float = 0.40,
) -> WPrimeResult:
    """
    Track W' balance over the course of an activity.

    Uses a first-order recovery model:
      dW'/dt = -excess_power + (W'_balance / tau) * recovery_rate

    Args:
        activity_id: Intervals.icu activity ID.
        power_samples: Power in watts at each second.
        cp_estimate: Critical power estimate in watts. If None, uses NP.
        w_prime_capacity: Estimated W' capacity in kJ. If None, estimated from data.
        tau: W' recovery time constant in seconds (default 240s).
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

    for i, p in enumerate(power):
        excess = max(p - cp_estimate, 0.0)  # watts above CP
        drawdown = excess / 1000.0  # convert to kJ per second

        # Recovery: exponential reconstitution
        recovery = (w_prime_capacity - balance) / tau

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