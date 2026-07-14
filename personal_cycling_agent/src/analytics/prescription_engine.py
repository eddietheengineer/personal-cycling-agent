"""
Prescription engine: 3-index scoring, pain veto, edge case overrides, hard guardrails.

Implements the architecture from docs/TRAINING_PRESCRIPTION.md Parts 3, 7, 8, 22, 23.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ReadinessIndex:
    """Three-index readiness scoring."""
    subjective: float = 0.0  # 0-1
    autonomic: float = 0.0   # 0-1
    fitness: float = 0.0     # 0-1
    composite: float = 0.0   # 0-1


@dataclass
class PrescriptionOutput:
    """Structured prescription output with explanation."""
    readiness_assessment: dict[str, Any] = field(default_factory=dict)
    daily_plan: list[dict[str, Any]] = field(default_factory=list)
    safety_notes: list[dict[str, Any]] = field(default_factory=list)
    context_modifications: dict[str, float] = field(default_factory=dict)


@dataclass
class PrescriptionInput:
    """All inputs needed for prescription."""
    # Autonomic
    rmssd: float | None = None
    rmssd_baseline: float | None = None
    rmssd_std: float | None = None
    resting_hr: float | None = None
    rhr_baseline: float | None = None
    rhr_std: float | None = None
    dfa_a1: float | None = None

    # Subjective
    prs: float | None = None          # 0-10
    soreness: float | None = None     # 1-7
    stress: float | None = None       # 1-7
    sleep_quality: float | None = None  # 1-7
    mood: float | None = None
    pain_score: float | None = None   # 0-10
    pain_location: str | None = None

    # Fitness
    ctl: float | None = None
    atl: float | None = None
    tsb: float | None = None
    acwr: float | None = None
    decoupling: float | None = None
    w_prime_balance: float | None = None  # 0-100%
    ftp_trend: float | None = None  # +1 improving, 0 stable, -1 declining

    # Context
    illness_active: bool = False
    illness_recovery_days: int = 0
    travel_fatigue: bool = False
    jet_lag_direction: str | None = None  # "east" or "west"
    jet_lag_days: int = 0
    altitude_m: float = 0.0
    altitude_days: int = 0

    # Training context
    planned_tss: float = 0.0
    planned_type: str = "endurance"
    yesterday_tss: float = 0.0
    yesterday_planned_tss: float = 0.0
    consecutive_sleep_debt: int = 0  # days with <5h sleep
    consecutive_high_stress: int = 0  # days with stress >= 6


def compute_readiness_index(inp: PrescriptionInput) -> ReadinessIndex:
    """
    Compute 3-index readiness score.

    Subjective_Index = 0.35×(PRS/10) + 0.25×(1-DOMS/7) + 0.20×(sleep_qual/7) + 0.20×(1-stress/7)
    Autonomic_Index = 0.50×HRV_norm + 0.30×(1-RHR_norm) + 0.20×DFA_a1_norm
    Fitness_Index = 0.30×CTL_norm + 0.25×(1-decoupling/10) + 0.20×W'_norm + 0.15×FTP_trend
    Composite = 0.40×Subjective + 0.30×Autonomic + 0.30×Fitness
    """
    idx = ReadinessIndex()

    # --- Subjective Index ---
    if inp.prs is not None:
        idx.subjective += 0.35 * (inp.prs / 10.0)
    if inp.soreness is not None:
        idx.subjective += 0.25 * (1.0 - inp.soreness / 7.0)
    if inp.sleep_quality is not None:
        idx.subjective += 0.20 * (inp.sleep_quality / 7.0)
    if inp.stress is not None:
        idx.subjective += 0.20 * (1.0 - inp.stress / 7.0)
    idx.subjective = np.clip(idx.subjective, 0, 1)

    # --- Autonomic Index ---
    if inp.rmssd is not None and inp.rmssd_baseline is not None and inp.rmssd_std is not None:
        hrv_z = (inp.rmssd - inp.rmssd_baseline) / max(inp.rmssd_std, 1)
        # Normalize: z=0 → 0.5, z=+2 → 1.0, z=-2 → 0.0
        idx.autonomic += 0.50 * np.clip(0.5 + hrv_z / 4.0, 0, 1)
    if inp.resting_hr is not None and inp.rhr_baseline is not None and inp.rhr_std is not None:
        rhr_z = (inp.resting_hr - inp.rhr_baseline) / max(inp.rhr_std, 1)
        # Lower RHR is better: z=0 → 0.5, z=-2 → 1.0, z=+2 → 0.0
        idx.autonomic += 0.30 * np.clip(0.5 - rhr_z / 4.0, 0, 1)
    if inp.dfa_a1 is not None:
        # DFA-a1: 0.5-1.5 range, higher = better recovery
        idx.autonomic += 0.20 * np.clip((inp.dfa_a1 - 0.5) / 1.0, 0, 1)
    idx.autonomic = np.clip(idx.autonomic, 0, 1)

    # --- Fitness Index ---
    if inp.ctl is not None:
        # Normalize CTL: 0-150 range → 0-1
        idx.fitness += 0.30 * np.clip(inp.ctl / 150.0, 0, 1)
    if inp.decoupling is not None:
        # Lower decoupling is better: 0-10% → 1.0-0.0
        idx.fitness += 0.25 * np.clip(1.0 - inp.decoupling / 10.0, 0, 1)
    if inp.w_prime_balance is not None:
        # Higher W' balance = less depleted = better
        idx.fitness += 0.20 * np.clip(inp.w_prime_balance / 100.0, 0, 1)
    if inp.ftp_trend is not None:
        # +1 → 1.0, 0 → 0.5, -1 → 0.0
        idx.fitness += 0.15 * np.clip((inp.ftp_trend + 1) / 2.0, 0, 1)
    idx.fitness = np.clip(idx.fitness, 0, 1)

    # --- Composite ---
    idx.composite = 0.40 * idx.subjective + 0.30 * idx.autonomic + 0.30 * idx.fitness
    idx.composite = np.clip(idx.composite, 0, 1)

    return idx


def apply_pain_veto(inp: PrescriptionInput) -> tuple[float, list[dict]]:
    """
    Pain gating (hard guardrail H1).

    Returns (tss_factor, safety_notes).
    """
    notes = []
    pain = inp.pain_score or 0

    if pain >= 7:
        return 0.0, [{"severity": "critical", "message": f"Pain {pain}/10 — mandatory rest", "action": "rest"}]
    elif pain >= 5:
        notes.append({"severity": "critical", "message": f"Pain {pain}/10 — non-weight-bearing only", "action": "reduce_50"})
        return 0.5, notes
    elif pain >= 3:
        notes.append({"severity": "warning", "message": f"Pain {pain}/10 — reduce load, consider modality switch", "action": "reduce_30"})
        return 0.7, notes
    else:
        return 1.0, notes


def apply_edge_case_overrides(inp: PrescriptionInput) -> tuple[float, list[dict]]:
    """
    Edge case override system (Part 22).

    Returns (tss_factor, safety_notes).
    """
    notes = []
    factor = 1.0

    # Illness active
    if inp.illness_active:
        factor = min(factor, 0.0)
        notes.append({"severity": "critical", "message": "Active illness — no structured training", "action": "rest"})

    # Illness recovery
    elif inp.illness_recovery_days > 0:
        if inp.illness_recovery_days <= 3:
            factor = min(factor, 0.3)
            notes.append({"severity": "warning", "message": f"Illness recovery day {inp.illness_recovery_days} — 30% load, Z1-2 only", "action": "reduce_70"})
        elif inp.illness_recovery_days <= 7:
            factor = min(factor, 0.5)
            notes.append({"severity": "warning", "message": f"Illness recovery day {inp.illness_recovery_days} — 50% load", "action": "reduce_50"})
        else:
            factor = min(factor, 0.75)
            notes.append({"severity": "info", "message": f"Illness recovery day {inp.illness_recovery_days} — gradual return", "action": "reduce_25"})

    # Travel fatigue
    elif inp.travel_fatigue:
        factor = min(factor, 0.3)
        notes.append({"severity": "warning", "message": "Travel fatigue — 30% load, Z1-2, ≤60min", "action": "reduce_70"})

    # Jet lag
    elif inp.jet_lag_direction is not None and inp.jet_lag_days > 0:
        if inp.jet_lag_direction == "east":
            # Eastward: ~1.5 days/timezone
            recovery_day = inp.jet_lag_days / 1.5
            factor = min(factor, max(0.3, 1.0 - 0.7 / max(recovery_day, 1)))
        else:
            # Westward: ~0.5 days/timezone
            recovery_day = inp.jet_lag_days / 0.5
            factor = min(factor, max(0.5, 1.0 - 0.5 / max(recovery_day, 1)))
        notes.append({"severity": "warning", "message": f"Jet lag ({inp.jet_lag_direction}) day {inp.jet_lag_days}", "action": "reduce"})

    # Altitude acute
    elif inp.altitude_m > 2500 and inp.altitude_days <= 3:
        factor = min(factor, 0.3)
        notes.append({"severity": "warning", "message": f"Acute altitude ({int(inp.altitude_m)}m) day {inp.altitude_days} — 30% load", "action": "reduce_70"})
    elif inp.altitude_m > 2500 and inp.altitude_days <= 10:
        factor = min(factor, 0.5 + 0.04 * inp.altitude_days)
        notes.append({"severity": "info", "message": f"Altitude acclimatization day {inp.altitude_days}", "action": "moderate"})

    # Life stress chronic
    elif inp.consecutive_high_stress >= 3:
        factor = min(factor, 0.5)
        notes.append({"severity": "warning", "message": f"Chronic life stress ({inp.consecutive_high_stress} days) — 50% load, no intervals", "action": "reduce_50"})
    elif inp.consecutive_high_stress >= 1:
        factor = min(factor, 0.7)
        notes.append({"severity": "info", "message": f"Life stress ({inp.consecutive_high_stress} days) — reduce load", "action": "reduce_30"})

    return factor, notes


def apply_hard_guardrails(inp: PrescriptionInput) -> tuple[float, list[dict]]:
    """
    Hard guardrails (Part 23).

    Returns (tss_factor, safety_notes).
    """
    notes = []
    factor = 1.0

    # H3: rmssd < 50% of baseline → drop 2 zones
    if inp.rmssd is not None and inp.rmssd_baseline is not None:
        if inp.rmssd < 0.5 * inp.rmssd_baseline:
            factor = min(factor, 0.3)
            notes.append({"severity": "critical", "message": "HRV crash (<50% baseline) — drop intensity 2 zones", "action": "drop_2_zones"})

    # H4: sleep < 5h for 3+ consecutive days → cap TSS 60%
    if inp.consecutive_sleep_debt >= 3:
        factor = min(factor, 0.6)
        notes.append({"severity": "warning", "message": f"Sleep debt ({inp.consecutive_sleep_debt} days) — cap TSS at 60%", "action": "cap_60"})

    # H5: yesterday TSS > 1.5× planned → today Z2
    if inp.yesterday_planned_tss > 0:
        if inp.yesterday_tss > 1.5 * inp.yesterday_planned_tss:
            factor = min(factor, 0.5)
            notes.append({"severity": "warning", "message": "Yesterday's TSS exceeded plan — today Z2 recovery", "action": "z2_only"})

    # H6: ACWR > 1.5 → reduce next week
    if inp.acwr is not None and inp.acwr > 1.5:
        factor = min(factor, 0.85)
        notes.append({"severity": "warning", "message": f"ACWR {inp.acwr:.2f} — injury risk elevated", "action": "reduce_15"})

    return factor, notes


def generate_prescription(inp: PrescriptionInput) -> PrescriptionOutput:
    """
    Generate a complete training prescription.

    Pipeline:
    1. Compute readiness index (3-index scoring)
    2. Apply pain veto (hard stop)
    3. Apply edge case overrides
    4. Apply hard guardrails
    5. Generate daily plan with load adjustment
    6. Attach safety notes and explanations
    """
    output = PrescriptionOutput()

    # Step 1: Readiness index
    idx = compute_readiness_index(inp)
    readiness_score = int(idx.composite * 100)

    # Determine limiting factor
    if idx.subjective < idx.autonomic and idx.subjective < idx.fitness:
        limiting = "subjective"
    elif idx.autonomic < idx.fitness:
        limiting = "autonomic"
    else:
        limiting = "fitness"

    output.readiness_assessment = {
        "composite_score": readiness_score,
        "subjective_index": round(idx.subjective, 3),
        "autonomic_index": round(idx.autonomic, 3),
        "fitness_index": round(idx.fitness, 3),
        "limiting_factor": limiting,
        "confidence": "high" if inp.prs is not None else "low",
    }

    # Step 2: Pain veto
    pain_factor, pain_notes = apply_pain_veto(inp)
    output.safety_notes.extend(pain_notes)

    # Step 3: Edge case overrides
    edge_factor, edge_notes = apply_edge_case_overrides(inp)
    output.safety_notes.extend(edge_notes)

    # Step 4: Hard guardrails
    guardrail_factor, guardrail_notes = apply_hard_guardrails(inp)
    output.safety_notes.extend(guardrail_notes)

    # Combine all factors
    total_factor = pain_factor * edge_factor * guardrail_factor
    total_factor = np.clip(total_factor, 0, 1.0)

    # Step 5: Generate daily plan
    planned_tss = inp.planned_tss if inp.planned_tss > 0 else 80.0  # default 80 TSS
    adjusted_tss = planned_tss * total_factor

    # Determine session type based on readiness and factor
    if total_factor <= 0.1:
        session_type = "rest"
        target_zone = "Z1"
        duration = 0
    elif total_factor <= 0.3:
        session_type = "recovery"
        target_zone = "Z1"
        duration = 30
    elif total_factor <= 0.6:
        session_type = "endurance"
        target_zone = "Z2"
        duration = 60
    else:
        session_type = inp.planned_type
        target_zone = _select_zone(idx, inp)
        duration = int(60 * total_factor)

    # Build rationale
    rationale_parts = []
    if idx.composite >= 0.7:
        rationale_parts.append("Good readiness")
    elif idx.composite >= 0.4:
        rationale_parts.append("Moderate readiness")
    else:
        rationale_parts.append("Low readiness")

    if limiting == "subjective":
        rationale_parts.append("limited by subjective factors")
    elif limiting == "autonomic":
        rationale_parts.append("limited by autonomic state")
    else:
        rationale_parts.append("limited by fitness/fatigue balance")

    if total_factor < 1.0:
        rationale_parts.append(f"load reduced to {int(total_factor*100)}%")

    output.daily_plan = [{
        "session_type": session_type,
        "target_zone": target_zone,
        "duration_min": duration,
        "target_tss": round(adjusted_tss, 1),
        "load_adjustment": round((total_factor - 1.0) * 100, 1),
        "rationale": "; ".join(rationale_parts),
    }]

    # Context modifications
    output.context_modifications = {
        "pain_adjustment": round((pain_factor - 1.0) * 100, 1),
        "edge_case_adjustment": round((edge_factor - 1.0) * 100, 1),
        "guardrail_adjustment": round((guardrail_factor - 1.0) * 100, 1),
    }

    return output


def _select_zone(idx: ReadinessIndex, inp: PrescriptionInput) -> str:
    """Select target zone based on readiness and planned type."""
    if idx.composite < 0.4:
        return "Z2"
    elif idx.composite < 0.6:
        return "Z3"
    else:
        # Map planned type to zone
        zone_map = {
            "recovery": "Z1",
            "endurance": "Z2",
            "tempo": "Z3",
            "sweet_spot": "Z3-Z4",
            "threshold": "Z4",
            "vo2max": "Z5",
            "intervals": "Z4-Z5",
        }
        return zone_map.get(inp.planned_type, "Z2")