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
    rider_weight = os.getenv("RIDER_WEIGHT_KG", "unknown")

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
        for act in recent_activities[:5]:
            activity_lines.append(
                f"- {act.get('start_date', '?')}: {act.get('activity_type', '?')} "
                f"for {act.get('duration', '?')}s, "
                f"avg power {act.get('average_power', '?')}W, "
                f"TSS {act.get('tss', '?')}"
            )
        sections.append(f"\n## Recent Activities\n" + "\n".join(activity_lines))

    # Instruction
    sections.append(
        "\n## Instruction\n"
        "Based on the data above, prescribe today's training session. Include:\n"
        "1. Session type and duration\n"
        "2. Target power zones and wattage ranges\n"
        "3. Heart rate guidance\n"
        "4. Warm-up and cool-down structure\n"
        "5. Recovery notes\n"
        "6. Any adjustments from the planned session based on readiness"
    )

    return "\n".join(sections)


def build_json_context(
    readiness: dict[str, Any] | None = None,
    thresholds: list[dict[str, Any]] | None = None,
    w_prime: list[dict[str, Any]] | None = None,
    durability: list[dict[str, Any]] | None = None,
    decoupling: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build a compact JSON context for structured LLM consumption.

    Returns a JSON string with all analytics data.
    """
    context: dict[str, Any] = {
        "readiness": readiness,
        "thresholds": thresholds or [],
        "w_prime": w_prime or [],
        "durability": durability or [],
        "decoupling": decoupling or [],
    }
    return json.dumps(context, indent=2)