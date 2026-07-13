"""
Pmax estimation and Strain Score calculation.

Pmax (peak power) is estimated from PDC data with sanity checks.
Strain Score (SS) decomposes training load into energy-system-specific
strains (aerobic, glycolytic, alactic) per Kontro et al. 2026.

Based on:
- Kontro et al. 2026 (PLOS One): 3D IR model with Strain Score
- Puchowicz et al. 2020: Omni-domain CP model for Pmax estimation
"""

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PmaxResult:
    """Pmax estimation result."""
    pmax: float  # Estimated peak power in watts
    method: str  # How Pmax was estimated
    confidence: str  # high/medium/low based on data quality


@dataclass
class StrainScoreResult:
    """Strain Score decomposition for a single activity."""
    ss_total: float  # Total strain score
    ss_cp: float  # Aerobic strain (SS_CP)
    ss_wp: float  # Glycolytic strain (SS_W')
    ss_pmax: float  # Alactic strain (SS_Pmax)
    tss_equivalent: float  # TSS equivalent for comparison


def estimate_pmax(
    pdc: dict[int, float],
    cp: float,
    w_prime: float,
) -> PmaxResult:
    """
    Estimate Pmax from power-duration curve data.

    Uses 3s or 5s best power (more reliable than 1s sensor spikes).
    Clamps to reasonable range (2x-10x CP).

    Args:
        pdc: Power-duration curve (duration_sec -> best watts).
        cp: Critical power in watts.
        w_prime: W' capacity in joules.

    Returns:
        PmaxResult with estimated Pmax and confidence.
    """
    p1s = pdc.get(1, 0)
    p3s = pdc.get(3, 0)
    p5s = pdc.get(5, 0)

    if p1s == 0 and p3s == 0 and p5s == 0:
        return PmaxResult(pmax=0.0, method="no_data", confidence="low")

    # Sanity: Pmax is typically 3-8x CP; >10x is likely sensor error
    max_reasonable = cp * 10.0

    # Prefer 5s > 3s > 1s (longer = more reliable)
    if p5s > 0 and p5s < max_reasonable:
        pmax_est = p5s
        method = "pdc_5s"
        confidence = "high"
    elif p3s > 0 and p3s < max_reasonable:
        pmax_est = p3s
        method = "pdc_3s"
        confidence = "medium"
    elif p1s > 0 and p1s < max_reasonable:
        pmax_est = p1s
        method = "pdc_1s"
        confidence = "low"
    else:
        # All short-duration powers suspicious — use model prediction
        pmax_est = cp + w_prime
        method = "model_prediction"
        confidence = "low"

    # Clamp to reasonable range
    pmax = max(cp * 2.0, min(pmax_est, max_reasonable))

    return PmaxResult(pmax=round(pmax, 1), method=method, confidence=confidence)


def compute_strain_score(
    power_samples: list[float],
    duration: int,
    cp: float,
    w_prime: float,
    pmax: float,
    ftp: float,
) -> StrainScoreResult:
    """
    Compute Strain Score (SS) decomposition for an activity.

    Decomposes load into energy-system-specific strains:
    - SS_CP: Aerobic system load (power <= CP)
    - SS_W': Glycolytic system load (CP < power <= 1.5*CP)
    - SS_Pmax: Alactic system load (power > 1.5*CP)

    Formula (Kontro et al. 2026):
    k_strain = (Pmax - MPA + CP) / (Pmax - P + CP)
    SR = k_strain * P  (strain rate in watts)
    SS = sum(SR * Pmax / CP^2 * 100 / 3600)  (normalized)

    Args:
        power_samples: List of power values in watts (1Hz).
        duration: Duration in seconds.
        cp: Critical power in watts.
        w_prime: W' capacity in joules.
        pmax: Peak power in watts.
        ftp: Functional threshold power in watts.

    Returns:
        StrainScoreResult with decomposed strain scores.
    """
    if not power_samples or cp <= 0 or pmax <= cp:
        return StrainScoreResult(0.0, 0.0, 0.0, 0.0, 0.0)

    power = np.array(power_samples, dtype=float)
    mask = power > 0
    if not np.any(mask):
        return StrainScoreResult(0.0, 0.0, 0.0, 0.0, 0.0)

    mpa = cp  # Simplified: MPA ≈ CP

    # Strain coefficient
    numerator = pmax - mpa + cp
    denominator = np.maximum(pmax - power + cp, 1.0)
    k_strain = numerator / denominator
    sr = k_strain * power  # strain rate (W)

    # Energy system masks
    alactic = power > 1.5 * cp
    glycolytic = (power > cp) & (power <= 1.5 * cp)
    aerobic = power <= cp

    # Normalization factor
    norm = pmax / (cp ** 2) * 100.0 / 3600.0

    ss_pmax = float(np.sum(sr[alactic]) * norm) if np.any(alactic) else 0.0
    ss_wp = float(np.sum(sr[glycolytic]) * norm) if np.any(glycolytic) else 0.0
    ss_cp = float(np.sum(sr[aerobic]) * norm) if np.any(aerobic) else 0.0
    ss_total = ss_cp + ss_wp + ss_pmax

    # TSS equivalent
    if ftp > 0 and len(power) > 0:
        np_val = _compute_normalized_power(power)
        if_val = np_val / ftp
        tss_eq = (len(power) / 3600.0 * np_val * if_val) / ftp * 100.0
    else:
        tss_eq = 0.0

    return StrainScoreResult(
        ss_total=round(ss_total, 2),
        ss_cp=round(ss_cp, 2),
        ss_wp=round(ss_wp, 2),
        ss_pmax=round(ss_pmax, 2),
        tss_equivalent=round(tss_eq, 2),
    )


def _compute_normalized_power(power: np.ndarray) -> float:
    """Compute Normalized Power from power samples."""
    if len(power) < 4:
        return float(np.mean(power))
    cumsum = np.cumsum(power)
    cumsum = np.insert(cumsum, 0, 0.0)
    ma = (cumsum[4:] - cumsum[:-4]) / 4.0
    return float(np.mean(ma ** 4)) ** 0.25


def strain_score_to_dict(result: StrainScoreResult) -> dict[str, Any]:
    """Serialize StrainScoreResult to a plain dict."""
    return {
        "ss_total": result.ss_total,
        "ss_cp": result.ss_cp,
        "ss_wp": result.ss_wp,
        "ss_pmax": result.ss_pmax,
        "tss_equivalent": result.tss_equivalent,
    }


def pmax_to_dict(result: PmaxResult) -> dict[str, Any]:
    """Serialize PmaxResult to a plain dict."""
    return {
        "pmax": result.pmax,
        "method": result.method,
        "confidence": result.confidence,
    }