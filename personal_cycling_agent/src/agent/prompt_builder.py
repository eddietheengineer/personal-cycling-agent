"""
Prompt Builder for the cycling AI agent.

Reads the user profile, today's readiness state, and recent analytics
to construct a structured system prompt for the local LLM.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from src import config

_initialized = False


def _ensure_init() -> None:
    """Lazily initialize config on first use."""
    global _initialized
    if not _initialized:
        config.setup()
        _initialized = True

logger = logging.getLogger(__name__)


def load_user_profile() -> str:
    """Load the user profile markdown file. Returns empty string if not found."""
    _ensure_init()
    profile_path = config.user_profile_path()
    if not profile_path.exists():
        logger.warning(f"User profile not found at {profile_path}")
        return ""
    with open(profile_path, "r", encoding="utf-8") as f:
        return f.read()


def build_system_prompt(
    readiness: dict[str, Any] | None = None,
    thresholds: list[dict[str, Any]] | None = None,
    w_prime: list[dict[str, Any]] | None = None,
    durability: list[dict[str, Any]] | None = None,
    decoupling: list[dict[str, Any]] | None = None,
    recent_activities: list[dict[str, Any]] | None = None,
    analysis: dict[str, Any] | None = None,
) -> str:
    """
    Build a complete system prompt for the LLM.

    Args:
        readiness: Dict from readiness_to_dict().
        thresholds: List of dicts from threshold_to_dict().
        w_prime: List of dicts from w_prime_to_dict().
        durability: List of dicts from durability_to_dict().
        decoupling: List of dicts from decoupling_to_dict().
        recent_activities: List of recent activity summary dicts.

    Returns:
        A formatted system prompt string.
    """
    profile = load_user_profile()
    today = datetime.now().strftime("%Y-%m-%d")
    rider_weight = os.getenv("WEIGHT_KG", "unknown")

    # Build sections
    sections = [
        "You are an expert cycling coach AI. Your job is to prescribe daily training "
        "based on the rider's physiological data, training history, and goals. "
        "Be specific with wattage targets, durations, and recovery guidance.",
    ]

    # Rider profile
    if profile:
        sections.append(f"\n## Rider Profile\n{profile}")
    else:
        sections.append("\n## Rider Profile\nNo user profile loaded. Use data-driven defaults.")

    # Biometrics
    sections.append(f"\n## Biometrics\n- Weight: {rider_weight} kg\n- Date: {today}")

    # Readiness state
    if readiness:
        state = readiness.get("state", "unknown")
        recommendation = readiness.get("recommendation", "")
        rmssd = readiness.get("rmssd")
        rhr = readiness.get("resting_hr")
        sections.append(
            f"\n## Today's Readiness\n"
            f"- State: **{state}**\n"
            f"- RMSSD: {rmssd} (baseline: {readiness.get('rmssd_band', 'N/A')})\n"
            f"- RHR: {rhr} (baseline: {readiness.get('rhr_band', 'N/A')})\n"
            f"- Assessment: {recommendation}"
        )
    else:
        sections.append("\n## Today's Readiness\nNo readiness data available.")

    # Thresholds
    if thresholds:
        latest = thresholds[-1]  # most recent
        lt1 = latest.get("lt1_power")
        lt2 = latest.get("lt2_power")
        sections.append(
            f"\n## Thresholds (latest activity)\n"
            f"- LT1 (Aerobic): {lt1}W\n"
            f"- LT2 (Critical Power): {lt2}W\n"
            f"- Zone 2 Audit: {'PASSED' if latest.get('zone2_audit_passed') else 'FAILED'} "
            f"({latest.get('zone2_violation_pct', 0):.1%} below LT1)"
        )

    # W' tracking
    if w_prime:
        latest = w_prime[-1]
        sections.append(
            f"\n## W' (Anaerobic Capacity)\n"
            f"- Capacity: {latest.get('w_prime_capacity', 'N/A')} kJ\n"
            f"- Min Balance: {latest.get('min_balance_pct', 0):.1%}\n"
            f"- Progression: {'RECOMMENDED' if latest.get('progression_recommended') else 'HOLD'}"
        )

    # Durability
    if durability:
        latest = durability[-1]
        sections.append(
            f"\n## Durability\n"
            f"- Total Energy: {latest.get('total_kj', 'N/A')} kJ\n"
            f"- 1min Degradation: {latest.get('degradation_1min', 'N/A')}%\n"
            f"- 5min Degradation: {latest.get('degradation_5min', 'N/A')}%"
        )

    # Decoupling
    if decoupling:
        latest = decoupling[-1]
        sections.append(
            f"\n## Aerobic Decoupling\n"
            f"- Drift: {latest.get('drift_pct', 'N/A')}%\n"
            f"- Duration Increase: {'APPROVED' if latest.get('increase_duration_recommended') else 'HOLD'}"
        )

    # Recent activities context
    if recent_activities and len(recent_activities) > 0:
        activity_lines = []
        for act in recent_activities:
            activity_lines.append(
                f"- {act.get('start_date', '?')}: {act.get('activity_name', '') or act.get('activity_type', '?')} "
                f"({act.get('duration', 0)/60:.0f}min, "
                f"avg power {act.get('average_power', '?')}W, "
                f"TSS {act.get('tss', '?')}, "
                f"IF {act.get('intensity_factor', '?')})"
            )
        sections.append(f"\n## Recent Activities\n" + "\n".join(activity_lines))

    # Full analysis data (if available)
    if analysis:
        analysis_sections = []

        # Training load trend
        tlh = analysis.get("training_load_history", [])
        if tlh:
            lines = ["## Training Load (last 14 days)"]
            for e in tlh[-14:]:
                lines.append(f"  {e['date']}: CTL={e['ctl']:.1f} ATL={e['atl']:.1f} TSB={e['tsb']:.1f}")
            analysis_sections.append("\n".join(lines))

        # Current training load
        tl = analysis.get("training_load", {})
        if tl:
            analysis_sections.append(
                f"## Current Training Load: CTL={tl.get('ctl',0):.1f} ATL={tl.get('atl',0):.1f} TSB={tl.get('tsb',0):.1f}"
            )

        # Recent power metrics
        pm = analysis.get("power_metrics", [])
        if pm:
            lines = ["## Recent Power Metrics (last 14)"]
            for e in pm[-14:]:
                lines.append(
                    f"  {e['activity_id']}: NP={e['normalized_power']:.0f} IF={e['intensity_factor']:.2f} "
                    f"TSS={e['tss']:.0f} VI={e['variability_index']:.2f}"
                )
            analysis_sections.append("\n".join(lines))

        # Recent strain scores
        ss = analysis.get("strain_scores", [])
        if ss:
            lines = ["## Recent Strain Scores (last 14)"]
            for e in ss[-14:]:
                lines.append(
                    f"  SS_total={e['ss_total']:.0f} SS_cp={e['ss_cp']:.0f} "
                    f"SS_wp={e['ss_wp']:.0f} TSS_eq={e['tss_equivalent']:.0f}"
                )
            analysis_sections.append("\n".join(lines))

        # Recent W'
        wp = analysis.get("w_prime", [])
        if wp:
            lines = ["## Recent W' Balance (last 14)"]
            for e in wp[-14:]:
                lines.append(
                    f"  Capacity={e.get('w_prime_capacity',0):.1f}kJ "
                    f"MinBalance={e.get('min_balance_pct',0):.0f}%"
                )
            analysis_sections.append("\n".join(lines))

        # Recent durability
        dur = analysis.get("durability", [])
        if dur:
            lines = ["## Recent Durability (last 14)"]
            for e in dur[-14:]:
                lines.append(
                    f"  TotalKJ={e.get('total_kj',0):.0f} "
                    f"Deg1m={e.get('degradation_1min','N/A')}% "
                    f"Deg5m={e.get('degradation_5min','N/A')}%"
                )
            analysis_sections.append("\n".join(lines))

        # Recent decoupling
        dec = analysis.get("decoupling", [])
        if dec:
            lines = ["## Recent Decoupling (last 14)"]
            for e in dec[-14:]:
                lines.append(
                    f"  Drift={e.get('drift_pct','N/A')}% "
                    f"IncreaseDur={'APPROVED' if e.get('increase_duration_recommended') else 'HOLD'}"
                )
            analysis_sections.append("\n".join(lines))

        # Pmax
        pmax = analysis.get("pmax_estimates", [])
        if pmax:
            latest = pmax[-1]
            analysis_sections.append(f"## Pmax: {latest.get('pmax',0):.0f}W ({latest.get('method','?')})")

        # 3D IR
        ir = analysis.get("three_dim_ir", {})
        if ir:
            analysis_sections.append(f"## 3D IR Readiness: {ir.get('readiness_from_fitness',0):.1f}%")

        # CP
        cp = analysis.get("cp")
        if cp:
            analysis_sections.append(f"## Current CP: {cp:.0f}W")

        # Feedback
        fb = analysis.get("feedback", {})
        if fb:
            analysis_sections.append(f"## Coach Feedback: {fb.get('reason','')}")

        sections.append("\n".join(analysis_sections))

    # Instruction
    sections.append(
        "\n## Instruction\n"
        "You have access to detailed training data above. When the user asks about\n"
        "training history, patterns, or causes of issues, analyze the data provided.\n"
        "If you need more specific data, you can query the database by starting your\n"
        "response with `QUERY: <SQL SELECT statement>` — the system will execute it\n"
        "and return the results. Only use queries when the data above is insufficient.\n\n"
        "When prescribing training, include:\n"
        "1. Session type and duration\n"
        "2. Target power zones and wattage ranges\n"
        "3. Heart rate guidance\n"
        "4. Warm-up and cool-down structure\n"
        "5. Recovery notes\n"
        "6. Any adjustments from the planned session based on readiness"
    )

    return "\n".join(sections)