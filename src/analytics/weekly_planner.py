"""
Weekly training planner.

Generates a 7-day training plan considering:
- Current readiness state and training load (CTL/ATL/TSB)
- User availability schedule
- Weather forecast
- User goals from profile
- Recent training history

Produces structured daily plans that can be rendered as a calendar.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.config import vault_path
from src.config.schedule import load_schedule, get_available_days, get_time_slots
from src.services.weather import get_location, get_weekly_forecast

logger = logging.getLogger(__name__)


@dataclass
class DailyPlan:
    """A single day's training plan."""
    date: str  # ISO date string
    weekday: int  # 0=Mon..6=Sun
    rest_day: bool
    session_type: str  # "rest" | "recovery" | "endurance" | "threshold" | "vo2" | "anaerobic" | "mixed"
    target_zone: str  # "Z1" | "Z2" | "Z3" | "Z4" | "Z5" | "Z1-Z2" | "Z4-Z5"
    duration_min: int
    target_tss: float
    indoor: bool  # True if weather suggests indoor
    description: str  # Human-readable workout description
    weather_note: str = ""  # e.g. "Rain likely — indoor recommended"
    rationale: str = ""  # Why this workout was chosen


@dataclass
class WeeklyPlan:
    """A complete 7-day training plan."""
    week_start: str  # ISO date of Monday
    days: list[DailyPlan] = field(default_factory=list)
    weekly_tss_target: float = 0.0
    weekly_tss_planned: float = 0.0
    generated_at: str = ""
    readiness_summary: str = ""


def _load_analysis() -> dict[str, Any]:
    """Load latest analysis from vault."""
    path = vault_path() / "data" / "latest_analysis.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _load_profile() -> dict[str, Any]:
    """Parse user_profile.md into a dict."""
    path = vault_path() / "user_profile.md"
    if not path.exists():
        return {}
    profile = {}
    try:
        text = path.read_text()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("- "):
                parts = line[2:].split(":", 1)
                if len(parts) == 2:
                    key = parts[0].strip().lower().replace(" ", "_")
                    val = parts[1].strip()
                    profile[key] = val
    except Exception:
        pass
    return profile


def _parse_float(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _project_ctl_atl(current_ctl: float, current_atl: float, daily_tss: list[float]) -> tuple[list[float], list[float]]:
    """Project CTL/ATL forward given a sequence of daily TSS values.

    Uses EMA with half-lives: CTL=18 days, ATL=7 days.
    """
    import math

    w_ctl = math.exp(-math.log(2) / 18.0)
    w_atl = math.exp(-math.log(2) / 7.0)

    ctl = current_ctl
    atl = current_atl
    ctl_series = [ctl]
    atl_series = [atl]

    for tss in daily_tss:
        ctl = (1 - w_ctl) * ctl + w_ctl * tss
        atl = (1 - w_atl) * atl + w_atl * tss
        ctl_series.append(ctl)
        atl_series.append(atl)

    return ctl_series[1:], atl_series[1:]


def _weather_adjustment(forecast: dict | None) -> tuple[bool, str]:
    """Return (should_be_indoor, note) based on weather forecast."""
    if not forecast:
        return False, ""

    precip = forecast.get("precipitation_prob", 0)
    wind = forecast.get("wind_speed", 0)
    condition = forecast.get("condition", "clear")
    temp_max = forecast.get("temp_max", 20)
    temp_min = forecast.get("temp_min", 10)

    if condition in ("storm", "snow"):
        return True, f"{condition.title()} expected — indoor recommended"
    if precip > 60:
        return True, f"{precip}% chance of rain — indoor recommended"
    if wind > 30:
        return True, f"Wind {wind:.0f} km/h — indoor recommended"
    if temp_max > 35:
        return False, f"Hot ({temp_max:.0f}°C) — train early, hydrate well"
    if temp_min < -5:
        return False, f"Cold ({temp_min:.0f}°C) — warm up extra"

    if precip > 30:
        return False, f"{precip}% chance of rain — have indoor backup"
    return False, ""


def _select_session_type(
    day_index: int,
    readiness_score: float,
    tsb: float,
    projected_ctl: float,
    projected_atl: float,
    available_days: list[int],
    training_day_index: int,
) -> tuple[str, str, int, float]:
    """Select session type, zone, duration, and target TSS for a training day.

    Returns (session_type, target_zone, duration_min, target_tss).
    """
    # Readiness-based modulation
    if readiness_score < 40:
        return "recovery", "Z1", 30, 15.0
    if readiness_score < 55:
        return "endurance", "Z2", 45, 30.0

    # TSB-based adjustment
    if tsb < -15:
        # Fatigued — easy day
        return "recovery", "Z1-Z2", 40, 20.0
    if tsb > 15:
        # Fresh — hard day
        pass  # fall through to normal plan

    # Weekly distribution pattern for 3-day week: endurance / threshold / vo2
    # For more days, distribute more evenly
    num_days = len(available_days)
    if num_days == 0:
        return "rest", "Z1", 0, 0.0

    # Session types rotated across training days
    patterns = {
        1: ["endurance"],
        2: ["endurance", "threshold"],
        3: ["endurance", "threshold", "vo2"],
        4: ["endurance", "threshold", "endurance", "vo2"],
        5: ["recovery", "threshold", "endurance", "vo2", "anaerobic"],
        6: ["recovery", "threshold", "endurance", "vo2", "anaerobic", "mixed"],
    }
    session_types = patterns.get(min(num_days, 6), ["endurance"] * num_days)
    session_type = session_types[training_day_index % len(session_types)]

    # Map session type to zone, duration, TSS
    session_map = {
        "recovery": ("Z1", 30, 15.0),
        "endurance": ("Z2", 60, 50.0),
        "threshold": ("Z3-Z4", 45, 80.0),
        "vo2": ("Z4-Z5", 45, 90.0),
        "anaerobic": ("Z5", 30, 60.0),
        "mixed": ("Z2-Z4", 60, 70.0),
    }
    zone, duration, tss = session_map.get(session_type, ("Z2", 60, 50.0))

    # Adjust TSS based on readiness
    tss *= max(0.5, readiness_score / 100.0)

    return session_type, zone, duration, round(tss, 1)


def _build_description(
    session_type: str,
    target_zone: str,
    duration_min: int,
    target_tss: float,
    indoor: bool,
    time_slot: str,
) -> str:
    """Build a human-readable workout description."""
    location = "Indoor" if indoor else "Outdoor"
    time_label = time_slot.capitalize()

    type_descriptions = {
        "recovery": f"Easy recovery spin in {target_zone}. Keep it relaxed.",
        "endurance": f"Steady endurance ride in {target_zone}. Build the base.",
        "threshold": f"Threshold work: warmup, 2x10min at {target_zone}, cool down.",
        "vo2": f"VO2 max intervals: warmup, 5x3min at {target_zone} with 3min easy, cool down.",
        "anaerobic": f"Anaerobic power: warmup, 8x30s max effort with 90s easy, cool down.",
        "mixed": f"Mixed session: base in {target_zone} with surges and hill repeats.",
    }

    desc = type_descriptions.get(session_type, f"Ride in {target_zone}")
    return f"{location} ({time_label}): {duration_min}min — {desc} (Target TSS: {target_tss:.0f})"


def generate_weekly_plan() -> WeeklyPlan:
    """Generate a complete 7-day training plan."""
    today = date.today()

    # Show today + next 6 days (not calendar week)
    week_start = today
    week_dates = [today + timedelta(days=i) for i in range(7)]

    # Load data
    analysis = _load_analysis()
    profile = _load_profile()
    schedule = load_schedule()
    available_days = get_available_days()

    # Current training load
    training_load = analysis.get("training_load", {})
    current_ctl = _parse_float(training_load.get("ctl"), 100.0)
    current_atl = _parse_float(training_load.get("atl"), 80.0)
    current_tsb = current_ctl - current_atl

    # Readiness
    readiness = analysis.get("readiness", {})
    readiness_score = _parse_float(readiness.get("composite_score"), 70.0)
    cp = _parse_float(analysis.get("cp"), 224.0)

    # Profile
    ftp = _parse_float(profile.get("ftp_(watts)"), cp)
    max_hr = _parse_float(profile.get("max_hr"), 190)
    weight = _parse_float(profile.get("weight_(kg)"), 81.0)
    max_duration = _parse_float(profile.get("max_session_duration"), 90.0)

    # Weather forecast
    forecast_map: dict[str, dict] = {}
    location = get_location()
    if location:
        forecasts = get_weekly_forecast(location[0], location[1])
        for f in forecasts:
            forecast_map[f.get("date", "")] = f

    # Score each available day by weather quality (lower precip = better)
    # Then pick the best days up to MAX_TRAINING_DAYS
    MAX_TRAINING_DAYS = 3

    day_scores: list[tuple[int, date, float, dict | None, list[str]]] = []
    for i, day_date in enumerate(week_dates):
        weekday = day_date.weekday()
        date_str = day_date.isoformat()
        if weekday not in available_days:
            continue
        time_slots = get_time_slots(weekday)
        day_forecast = forecast_map.get(date_str)

        # Score: 100 = perfect, 0 = terrible weather
        score = 100.0
        if day_forecast:
            precip = day_forecast.get("precipitation_prob", 0)
            condition = day_forecast.get("condition", "clear")
            if condition in ("storm", "snow"):
                score = 0
            elif condition == "rain":
                score = max(0, 100 - precip * 2)
            else:
                score = max(0, 100 - precip)

        day_scores.append((i, day_date, score, day_forecast, time_slots))

    # Sort by score descending, pick top MAX_TRAINING_DAYS
    day_scores.sort(key=lambda x: -x[2])
    training_days = set()
    for entry in day_scores:
        if len(training_days) >= MAX_TRAINING_DAYS:
            break
        # Skip days with terrible weather (score < 20) unless we have no other choice
        if entry[2] < 20 and len(training_days) < len(day_scores):
            continue
        training_days.add(entry[0])

    # If we skipped too many due to weather, fill remaining slots
    if len(training_days) < MAX_TRAINING_DAYS:
        for entry in day_scores:
            if len(training_days) >= MAX_TRAINING_DAYS:
                break
            if entry[0] not in training_days:
                training_days.add(entry[0])

    # Build daily plans
    days: list[DailyPlan] = []
    training_day_counter = 0
    weekly_tss = 0.0

    # Session pattern for 3-day week
    session_pattern = ["endurance", "threshold", "vo2"]

    for i, day_date in enumerate(week_dates):
        weekday = day_date.weekday()
        date_str = day_date.isoformat()
        time_slots = get_time_slots(weekday)
        day_forecast = forecast_map.get(date_str)
        indoor, weather_note = _weather_adjustment(day_forecast)

        if i not in training_days:
            days.append(DailyPlan(
                date=date_str,
                weekday=weekday,
                rest_day=True,
                session_type="rest",
                target_zone="—",
                duration_min=0,
                target_tss=0.0,
                indoor=False,
                description="Rest day",
                weather_note=weather_note,
                rationale="Not selected for this week's plan",
            ))
            continue

        # Use today's readiness for today, project forward for future days
        day_readiness = readiness_score if day_date == today else max(50, readiness_score + (i - 1) * 3)
        day_readiness = min(90, day_readiness)

        # Projected TSB for this day
        proj_tsb = current_tsb + i * 2

        # Pick session type from pattern
        session_type = session_pattern[training_day_counter % len(session_pattern)]

        # Readiness-based override
        if day_readiness < 40:
            session_type = "recovery"
        elif day_readiness < 55:
            session_type = "endurance"
        elif proj_tsb < -15:
            session_type = "recovery"

        session_map = {
            "recovery": ("Z1", 30, 15.0),
            "endurance": ("Z2", 60, 50.0),
            "threshold": ("Z3-Z4", 45, 80.0),
            "vo2": ("Z4-Z5", 45, 90.0),
            "anaerobic": ("Z5", 30, 60.0),
            "mixed": ("Z2-Z4", 60, 70.0),
        }
        zone, duration, tss = session_map.get(session_type, ("Z2", 60, 50.0))

        # Adjust TSS based on readiness
        tss *= max(0.5, day_readiness / 100.0)

        # Cap duration
        duration = min(duration, int(max_duration))

        description = _build_description(session_type, zone, duration, tss, indoor, time_slots[0])

        rationale_parts = []
        if day_date == today:
            rationale_parts.append(f"Today's readiness: {readiness_score:.0f}/100")
        rationale_parts.append(f"Projected TSB: {proj_tsb:.0f}")
        if weather_note:
            rationale_parts.append(weather_note)
        rationale_parts.append("Selected as one of 3 best weather days")

        days.append(DailyPlan(
            date=date_str,
            weekday=weekday,
            rest_day=False,
            session_type=session_type,
            target_zone=zone,
            duration_min=duration,
            target_tss=round(tss, 1),
            indoor=indoor,
            description=description,
            weather_note=weather_note,
            rationale="; ".join(rationale_parts),
        ))

        weekly_tss += tss
        training_day_counter += 1

    # Compute weekly TSS target from CTL
    weekly_tss_target = current_ctl * 7 / 30

    # Scale sessions to hit weekly TSS target
    if weekly_tss > 0 and weekly_tss > weekly_tss_target:
        scale = weekly_tss_target / weekly_tss
        for day in days:
            if not day.rest_day:
                day.target_tss = round(day.target_tss * scale, 1)
                day.duration_min = max(20, int(day.duration_min * scale))
        weekly_tss = weekly_tss_target

    plan = WeeklyPlan(
        week_start=week_start.isoformat(),
        days=days,
        weekly_tss_target=round(weekly_tss_target, 1),
        weekly_tss_planned=round(weekly_tss, 1),
        generated_at=today.isoformat() + "T" + "__TIME__",
        readiness_summary=f"Readiness {readiness_score:.0f}/100, CTL {current_ctl:.0f}, ATL {current_atl:.0f}, TSB {current_tsb:.0f}",
    )

    return plan


def save_weekly_plan(plan: WeeklyPlan) -> None:
    """Save weekly plan to vault."""
    path = vault_path() / "data" / "latest_weekly_plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "week_start": plan.week_start,
        "weekly_tss_target": plan.weekly_tss_target,
        "weekly_tss_planned": plan.weekly_tss_planned,
        "generated_at": plan.generated_at,
        "readiness_summary": plan.readiness_summary,
        "days": [asdict(d) for d in plan.days],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_weekly_plan() -> WeeklyPlan | None:
    """Load weekly plan from vault. Returns None if not found or stale."""
    path = vault_path() / "data" / "latest_weekly_plan.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        # Check if plan start date is today or in the future
        plan_start = date.fromisoformat(data["week_start"])
        today = date.today()
        if plan_start < today:
            return None  # stale — plan starts before today
        days = [DailyPlan(**d) for d in data.get("days", [])]
        return WeeklyPlan(
            week_start=data["week_start"],
            days=days,
            weekly_tss_target=data.get("weekly_tss_target", 0),
            weekly_tss_planned=data.get("weekly_tss_planned", 0),
            generated_at=data.get("generated_at", ""),
            readiness_summary=data.get("readiness_summary", ""),
        )
    except Exception:
        return None