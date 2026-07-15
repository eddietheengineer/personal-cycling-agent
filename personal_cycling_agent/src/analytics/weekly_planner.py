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
from src.config.schedule import load_schedule, get_available_days, get_available_hours
from src.services.weather import get_location, get_weekly_forecast, find_ride_slot

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
    weather_temp_max: float = 0.0
    weather_temp_min: float = 0.0
    weather_precip: int = 0
    weather_condition: str = ""
    ride_note: str = ""  # e.g. "Ride 06:00-07:30 ✓" or "Only 2/5 hours clear"


@dataclass
class WeeklyPlan:
    """A complete 7-day training plan."""
    week_start: str  # ISO date of Monday
    days: list[DailyPlan] = field(default_factory=list)
    weekly_tss_target: float = 0.0
    weekly_tss_planned: float = 0.0
    generated_at: str = ""
    readiness_summary: str = ""
    ctl_series: list[float] = field(default_factory=list)
    atl_series: list[float] = field(default_factory=list)
    tsb_series: list[float] = field(default_factory=list)


@dataclass
class DaySlot:
    """Precomputed availability and weather for a single day."""
    date: str  # ISO date
    weekday: int  # 0=Mon..6=Sun
    available_hours: list[int]  # hours 0-23 the athlete is free
    is_available: bool  # True if athlete has any free time
    rideable: bool  # True if weather allows an outdoor ride
    ride_note: str  # e.g. "Ride 06:00-07:30 ✓" or "Rain blocks ride"
    rideable_hours: list[list[int]]  # contiguous clear hour blocks, e.g. [[6,7,8],[14,15]]
    forecast: dict | None  # raw weather forecast for this date


@dataclass
class PlanningContext:
    """Shared precomputed data for both AI and rules planners."""
    week_start: date
    week_dates: list[date]
    day_slots: list[DaySlot]  # one per day, indexed 0-6

    # Training load state
    current_ctl: float
    current_atl: float
    current_tsb: float
    cp: float  # critical power / FTP

    # Readiness
    readiness_score: float
    readiness_state: str
    readiness_recommendation: str

    # Profile
    max_duration_min: float
    tsb_floor: int
    available_weekdays: list[int]
    primary_goal: str


@dataclass
class PlanValidationError:
    """A single validation failure with a machine-readable code."""
    code: str  # e.g. "invalid_day", "tsb_floor", "outside_window"
    message: str  # human-readable explanation
    day_index: int | None = None  # which day (0-6) is affected


@dataclass
class PlanValidationResult:
    """Result of validating a proposed plan."""
    valid: bool
    errors: list[PlanValidationError] = field(default_factory=list)

    # Projected training load if plan is applied
    projected_ctl: list[float] = field(default_factory=list)
    projected_atl: list[float] = field(default_factory=list)
    projected_tsb: list[float] = field(default_factory=list)


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

    alpha_ctl = 1 - math.exp(-math.log(2) / 18.0)  # ~0.0378
    alpha_atl = 1 - math.exp(-math.log(2) / 7.0)   # ~0.0943

    ctl = current_ctl
    atl = current_atl
    ctl_series = []
    atl_series = []

    for tss in daily_tss:
        ctl = (1 - alpha_ctl) * ctl + alpha_ctl * tss
        atl = (1 - alpha_atl) * atl + alpha_atl * tss
        ctl_series.append(ctl)
        atl_series.append(atl)

    return ctl_series, atl_series



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
        return False, f"Hot ({temp_max*9/5+32:.0f}°F) — train early, hydrate well"
    if temp_min < -5:
        return False, f"Cold ({temp_min*9/5+32:.0f}°F) — warm up extra"

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


def build_planning_context() -> PlanningContext:
    """Build shared precomputed context for both planners."""
    today = date.today()
    week_start = today
    week_dates = [today + timedelta(days=i) for i in range(7)]

    analysis = _load_analysis()
    profile = _load_profile()
    schedule = load_schedule()
    available_days = get_available_days()

    training_load = analysis.get("training_load", {})
    current_ctl = _parse_float(training_load.get("ctl"), 100.0)
    current_atl = _parse_float(training_load.get("atl"), 80.0)
    current_tsb = current_ctl - current_atl

    readiness = analysis.get("readiness", {})
    readiness_score = _parse_float(readiness.get("composite_score"), 70.0)
    readiness_state = readiness.get("state", "")
    readiness_rec = readiness.get("recommendation", "")
    cp = _parse_float(analysis.get("cp"), 224.0)

    max_duration = _parse_float(profile.get("max_session_duration"), 90.0)
    tsb_floor = int(profile.get("tsb_floor", -10))
    primary_goal = profile.get("primary_goal", "VO2 max")

    # Weather
    forecast_map: dict[str, dict] = {}
    location = get_location()
    if location:
        forecasts = get_weekly_forecast(location[0], location[1])
        for f in forecasts:
            forecast_map[f.get("date", "")] = f

    # Build day slots
    ride_duration_hours = max_duration / 60.0
    day_slots: list[DaySlot] = []
    for i, day_date in enumerate(week_dates):
        weekday = day_date.weekday()
        date_str = day_date.isoformat()
        avail_hours = get_available_hours(weekday)
        day_forecast = forecast_map.get(date_str)

        rideable = False
        ride_note = ""
        rideable_hours: list[list[int]] = []
        if day_forecast and avail_hours:
            slot_start, ride_note = find_ride_slot(day_forecast, avail_hours, ride_duration_hours)
            rideable = slot_start is not None
            from src.services.weather import find_rideable_slots
            rideable_hours = find_rideable_slots(day_forecast, avail_hours)

        day_slots.append(DaySlot(
            date=date_str,
            weekday=weekday,
            available_hours=avail_hours,
            is_available=weekday in available_days,
            rideable=rideable,
            ride_note=ride_note,
            rideable_hours=rideable_hours,
            forecast=day_forecast,
        ))

    return PlanningContext(
        week_start=week_start,
        week_dates=week_dates,
        day_slots=day_slots,
        current_ctl=current_ctl,
        current_atl=current_atl,
        current_tsb=current_tsb,
        cp=cp,
        readiness_score=readiness_score,
        readiness_state=readiness_state,
        readiness_recommendation=readiness_rec,
        max_duration_min=max_duration,
        tsb_floor=tsb_floor,
        available_weekdays=available_days,
        primary_goal=primary_goal,
    )


def project_tsb(
    ctx: PlanningContext,
    daily_tss: list[float],
) -> tuple[list[float], list[float], list[float]]:
    """Project CTL, ATL, and TSB forward for each day given daily TSS values.

    Returns (ctl_series, atl_series, tsb_series) of length len(daily_tss).
    """
    import math
    alpha_ctl = 1 - math.exp(-math.log(2) / 18.0)
    alpha_atl = 1 - math.exp(-math.log(2) / 7.0)

    ctl = ctx.current_ctl
    atl = ctx.current_atl
    ctl_series, atl_series, tsb_series = [], [], []

    for tss in daily_tss:
        ctl = (1 - alpha_ctl) * ctl + alpha_ctl * tss
        atl = (1 - alpha_atl) * atl + alpha_atl * tss
        ctl_series.append(ctl)
        atl_series.append(atl)
        tsb_series.append(ctl - atl)

    return ctl_series, atl_series, tsb_series


def validate_plan(
    ctx: PlanningContext,
    days: list[DailyPlan],
) -> PlanValidationResult:
    """Validate a proposed plan against all hard constraints.

    Checks:
    - Plan covers exactly 7 days within the planning window
    - Training days are on available weekdays
    - Training days have rideable weather (or are marked indoor)
    - Projected TSB never drops below the floor
    - 1-3 training days total
    - Duration within max session limit
    - Session type appropriate for readiness
    """
    errors: list[PlanValidationError] = []

    if len(days) != 7:
        errors.append(PlanValidationError(
            code="plan_length",
            message=f"Plan must have exactly 7 days, got {len(days)}",
        ))
        return PlanValidationResult(valid=False, errors=errors)

    # Check planning window
    for i, day in enumerate(days):
        expected_date = ctx.week_dates[i].isoformat()
        if day.date != expected_date:
            errors.append(PlanValidationError(
                code="outside_window",
                message=f"Day {i} has date {day.date}, expected {expected_date} (within 7-day window)",
                day_index=i,
            ))

    # Check available weekdays
    for i, day in enumerate(days):
        if day.rest_day:
            continue
        if day.weekday not in ctx.available_weekdays:
            day_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][day.weekday]
            errors.append(PlanValidationError(
                code="invalid_day",
                message=f"Day {i} ({day_name}) is not in available schedule",
                day_index=i,
            ))

    # Check rideable weather
    for i, day in enumerate(days):
        if day.rest_day:
            continue
        slot = ctx.day_slots[i]
        if not slot.rideable and not day.indoor:
            errors.append(PlanValidationError(
                code="weather",
                message=f"Day {i} ({slot.date}) is not rideable ({slot.ride_note}) and not marked indoor",
                day_index=i,
            ))

    # Check training day count
    train_count = sum(1 for d in days if not d.rest_day)
    if train_count < 1:
        errors.append(PlanValidationError(
            code="no_training",
            message="Plan must have at least 1 training day",
        ))
    if train_count > 3:
        errors.append(PlanValidationError(
            code="too_many_days",
            message=f"Plan has {train_count} training days, max is 3",
        ))

    # Check duration limits
    for i, day in enumerate(days):
        if day.rest_day:
            continue
        if day.duration_min > ctx.max_duration_min:
            errors.append(PlanValidationError(
                code="duration",
                message=f"Day {i} duration {day.duration_min}min exceeds max {ctx.max_duration_min:.0f}min",
                day_index=i,
            ))

    # Check readiness-appropriate session types
    if ctx.readiness_score < 60:
        for i, day in enumerate(days):
            if day.rest_day:
                continue
            if day.session_type in ("threshold", "vo2", "anaerobic"):
                errors.append(PlanValidationError(
                    code="readiness",
                    message=f"Day {i}: {day.session_type} not allowed at readiness {ctx.readiness_score:.0f} (use recovery/endurance only)",
                    day_index=i,
                ))

    # Project TSB and check floor
    daily_tss = [d.target_tss if not d.rest_day else 0.0 for d in days]
    ctl_s, atl_s, tsb_s = project_tsb(ctx, daily_tss)

    for i, tsb in enumerate(tsb_s):
        if tsb < ctx.tsb_floor:
            errors.append(PlanValidationError(
                code="tsb_floor",
                message=f"Day {i} projected TSB {tsb:.1f} below floor {ctx.tsb_floor}",
                day_index=i,
            ))

    return PlanValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        projected_ctl=ctl_s,
        projected_atl=atl_s,
        projected_tsb=tsb_s,
    )


def generate_weekly_plan() -> WeeklyPlan:
    """Generate a complete 7-day training plan using rules."""
    ctx = build_planning_context()
    today = date.today()

    profile = _load_profile()
    ftp = _parse_float(profile.get("ftp_(watts)"), ctx.cp)
    max_hr = _parse_float(profile.get("max_hr"), 190)
    weight = _parse_float(profile.get("weight_(kg)"), 81.0)

    # Score each available day by rideability
    scored: list[tuple[int, float, str]] = []
    for i, slot in enumerate(ctx.day_slots):
        if not slot.is_available:
            continue
        score = 100.0 if slot.rideable else -1.0
        scored.append((i, score, slot.ride_note))

    # Pick top 3 rideable days, fill with non-rideable if needed
    scored.sort(key=lambda x: -x[1])
    training_days: set[int] = set()
    for idx, score, _ in scored:
        if len(training_days) >= 3:
            break
        if score >= 0:
            training_days.add(idx)
    if len(training_days) < 3:
        for idx, score, _ in scored:
            if len(training_days) >= 3:
                break
            if idx not in training_days:
                training_days.add(idx)

    # Build daily plans
    days: list[DailyPlan] = []
    session_pattern = ["endurance", "threshold", "vo2"]
    session_map = {
        "recovery": ("Z1", 30, 15.0),
        "endurance": ("Z2", 60, 50.0),
        "threshold": ("Z3-Z4", 45, 80.0),
        "vo2": ("Z4-Z5", 45, 90.0),
        "anaerobic": ("Z5", 30, 60.0),
        "mixed": ("Z2-Z4", 60, 70.0),
    }
    training_day_counter = 0

    for i, day_date in enumerate(ctx.week_dates):
        slot = ctx.day_slots[i]
        weekday = day_date.weekday()
        date_str = day_date.isoformat()
        indoor, weather_note = _weather_adjustment(slot.forecast)

        if i not in training_days:
            days.append(DailyPlan(
                date=date_str, weekday=weekday, rest_day=True,
                session_type="rest", target_zone="—", duration_min=0,
                target_tss=0.0, indoor=False, description="Rest day",
                weather_note=weather_note,
                rationale="Not selected for this week's plan",
                weather_temp_max=slot.forecast.get("temp_max", 0) if slot.forecast else 0,
                weather_temp_min=slot.forecast.get("temp_min", 0) if slot.forecast else 0,
                weather_precip=slot.forecast.get("precipitation_prob", 0) if slot.forecast else 0,
                weather_condition=slot.forecast.get("condition", "") if slot.forecast else "",
                ride_note=slot.ride_note,
            ))
            continue

        # Project TSB to this point
        past_tss = [d.target_tss for d in days if not d.rest_day]
        _, _, proj_tsb = project_tsb(ctx, past_tss + [0.0])
        proj_tsb_val = proj_tsb[-1] if proj_tsb else ctx.current_tsb

        # Pick session type
        session_type = session_pattern[training_day_counter % len(session_pattern)]
        zone, duration, tss = session_map.get(session_type, ("Z2", 60, 50.0))

        # Adjust TSS for readiness
        day_readiness = ctx.readiness_score if day_date == today else max(50, min(90, ctx.readiness_score + (i - 1) * 3))
        tss *= max(0.5, day_readiness / 100.0)

        # Check TSB floor — downgrade if needed
        _, _, post_tsb = project_tsb(ctx, past_tss + [tss])
        if post_tsb[-1] < ctx.tsb_floor:
            zone, duration, tss = "Z1", 30, 15.0
            session_type = "recovery"
            _, _, post_tsb2 = project_tsb(ctx, past_tss + [tss])
            if post_tsb2[-1] < ctx.tsb_floor:
                days.append(DailyPlan(
                    date=date_str, weekday=weekday, rest_day=True,
                    session_type="rest", target_zone="—", duration_min=0,
                    target_tss=0.0, indoor=False, description="Rest day — TSB protection",
                    weather_note=weather_note,
                    rationale=f"Skipped: projected TSB {post_tsb[-1]:.0f} below floor {ctx.tsb_floor}",
                    weather_temp_max=slot.forecast.get("temp_max", 0) if slot.forecast else 0,
                    weather_temp_min=slot.forecast.get("temp_min", 0) if slot.forecast else 0,
                    weather_precip=slot.forecast.get("precipitation_prob", 0) if slot.forecast else 0,
                    weather_condition=slot.forecast.get("condition", "") if slot.forecast else "",
                    ride_note=slot.ride_note,
                ))
                training_day_counter += 1
                continue

        duration = min(duration, int(ctx.max_duration_min))
        avail = slot.available_hours
        window_label = f"{avail[0]:02d}:00-{avail[-1]+1:02d}:00" if avail else "morning"
        description = _build_description(session_type, zone, duration, tss, indoor, window_label)

        rationale_parts = []
        if day_date == today:
            rationale_parts.append(f"Today's readiness: {ctx.readiness_score:.0f}/100")
        rationale_parts.append(f"Projected TSB: {proj_tsb_val:.0f} (post: {post_tsb[-1]:.0f})")
        if weather_note:
            rationale_parts.append(weather_note)

        days.append(DailyPlan(
            date=date_str, weekday=weekday, rest_day=False,
            session_type=session_type, target_zone=zone,
            duration_min=duration, target_tss=round(tss, 1),
            indoor=indoor, description=description,
            weather_note=weather_note,
            rationale="; ".join(rationale_parts),
            weather_temp_max=slot.forecast.get("temp_max", 0) if slot.forecast else 0,
            weather_temp_min=slot.forecast.get("temp_min", 0) if slot.forecast else 0,
            weather_precip=slot.forecast.get("precipitation_prob", 0) if slot.forecast else 0,
            weather_condition=slot.forecast.get("condition", "") if slot.forecast else "",
            ride_note=slot.ride_note,
        ))
        training_day_counter += 1

    # Validate
    validation = validate_plan(ctx, days)
    if not validation.valid:
        logger.warning(f"Rules plan validation failed: {[e.message for e in validation.errors]}")

    # Scale to weekly TSS target
    weekly_tss_target = ctx.current_ctl * 7 / 30
    weekly_tss = sum(d.target_tss for d in days if not d.rest_day)
    if weekly_tss > 0 and weekly_tss > weekly_tss_target:
        scale = weekly_tss_target / weekly_tss
        for day in days:
            if not day.rest_day:
                day.target_tss = round(day.target_tss * scale, 1)
                day.duration_min = max(20, int(day.duration_min * scale))
        weekly_tss = weekly_tss_target

    # Project CTL/ATL/TSB
    daily_tss = [d.target_tss for d in days]
    ctl_s, atl_s, tsb_s = project_tsb(ctx, daily_tss)

    return WeeklyPlan(
        week_start=ctx.week_start.isoformat(),
        days=days,
        weekly_tss_target=round(weekly_tss_target, 1),
        weekly_tss_planned=round(weekly_tss, 1),
        generated_at=today.isoformat(),
        readiness_summary=f"Readiness {ctx.readiness_score:.0f}/100, CTL {ctx.current_ctl:.0f}, TSB {ctx.current_tsb:.0f}",
        ctl_series=[round(c, 1) for c in ctl_s],
        atl_series=[round(a, 1) for a in atl_s],
        tsb_series=[round(t, 1) for t in tsb_s],
    )
def generate_ai_plan() -> WeeklyPlan:
    """Generate a weekly plan using LLM with validation retry loop."""
    from src.agent.llm_client import generate

    ctx = build_planning_context()
    today = date.today()
    profile = _load_profile()

    day_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    available_days_str = ", ".join(day_names[i] for i in ctx.available_weekdays)

    # Build weather lines from context
    weather_lines = []
    for slot in ctx.day_slots:
        fc = slot.forecast or {}
        tmax_f = fc.get("temp_max", 0)
        tmin_f = fc.get("temp_min", 0)
        ride_tag = "RIDEABLE" if slot.rideable else "NOT_RIDEABLE"
        ride_note = slot.ride_note if slot.rideable else "weather blocks ride window"
        weather_lines.append(
            f"{slot.date}: {fc.get('condition','unknown')} {tmax_f:.0f}F/{tmin_f:.0f}F "
            f"precip {fc.get('precipitation_prob',0)}% [{ride_tag}: {ride_note}]"
        )

    # Journal context
    journal_context = ""
    try:
        from src.memory.journal import load_recent
        journal_context = load_recent(30)
    except Exception:
        pass

    def _fmt_hours(block: list[int]) -> str:
        if not block:
            return ""
        return f"{block[0]:02d}:00-{block[-1]+1:02d}:00"

    def _build_prompt(feedback: str = "") -> str:
        fb_block = ""
        if feedback:
            fb_block = f"\n## PREVIOUS ATTEMPT FAILED:\n{feedback}\nFix ALL errors above and return a corrected plan.\n\n"

        schedule_lines = []
        for i, slot in enumerate(ctx.day_slots):
            status = "AVAILABLE" if slot.is_available else "UNAVAILABLE"
            weather = "RIDEABLE" if slot.rideable else "INDOOR_ONLY"
            line = f"  Day {i} ({day_names[slot.weekday]} {slot.date}): {status}, {weather}"
            if slot.rideable_hours:
                windows = ", ".join(_fmt_hours(b) for b in slot.rideable_hours)
                line += f", rideable_windows=[{windows}]"
            schedule_lines.append(line)

        return (
            f"You are a cycling coach. Generate a 7-day training plan.\n\n"
            f"ATHLETE: Readiness {ctx.readiness_score:.0f}/100 ({ctx.readiness_state}), "
            f"CTL {ctx.current_ctl:.0f}, ATL {ctx.current_atl:.0f}, TSB {ctx.current_tsb:.0f}, "
            f"CP {ctx.cp:.0f}W\n"
            f"Recommendation: {ctx.readiness_recommendation}\n"
            f"Goals: {ctx.primary_goal}\n"
            f"Max session: {ctx.max_duration_min:.0f}min\n\n"
            f"## WEEK SCHEDULE (today + 6 days):\n"
            + "\n".join(schedule_lines)
            + f"\n\n## HARD CONSTRAINTS:\n"
            f"1. Return exactly 7 days, one per date above (day 0 = today).\n"
            f"2. Pick 1-3 training days. All others: rest_day=true, tss=0.\n"
            f"3. Training days MUST be on AVAILABLE weekdays: {available_days_str}.\n"
            f"4. Each day lists rideable_windows — contiguous clear time blocks for outdoor riding.\n"
            f"   If rideable_windows is empty, set indoor=true.\n"
            f"   Prefer days with more rideable windows for outdoor sessions.\n"
            f"5. Max duration: {ctx.max_duration_min:.0f}min per session.\n"
            f"6. TSB FLOOR: {ctx.tsb_floor}. Projected TSB must never drop below this.\n"
            f"   Current TSB: {ctx.current_tsb:.0f}. Each training day adds fatigue.\n"
            f"   Recovery (TSS~15) adds minimal fatigue; endurance (TSS~50) moderate; "
            f"threshold/VO2 (TSS~80-90) high.\n"
            f"7. If readiness < 60, use only recovery/endurance.\n"
            f"8. Total weekly TSS target: ~{ctx.current_ctl*7/30:.0f}.\n\n"
            f"WEATHER:\n" + "\n".join(weather_lines) + "\n\n"
            + journal_context
            + fb_block
            + 'Return ONLY a JSON array of 7 day objects:\n'
            + '[{"date":"YYYY-MM-DD","weekday":0-6,"rest_day":bool,"session_type":"rest|recovery|endurance|threshold|vo2|anaerobic|mixed",'
            + '"target_zone":"Z1-Z5","duration_min":int,"target_tss":float,"indoor":bool,'
            + '"description":"str","weather_note":"str","rationale":"str"}]\n'
        )

    def _parse_llm_response(response: str) -> list[dict] | None:
        import re
        json_match = re.search(r'\[[\s\S]*\]', response)
        if not json_match:
            return None
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            return None

    def _raw_to_days(raw_days: list[dict]) -> list[DailyPlan]:
        """Convert LLM JSON to DailyPlan objects, forcing dates to this week."""
        weekday_to_date = {d.weekday(): d.isoformat() for d in ctx.week_dates}
        days = []
        for rd in raw_days[:7]:
            weekday = rd.get("weekday", 0)
            ds = weekday_to_date.get(weekday, ctx.day_slots[weekday].date if weekday < 7 else "")
            slot = ctx.day_slots[weekday] if weekday < 7 else ctx.day_slots[0]
            fc = slot.forecast or {}
            indoor, weather_note = _weather_adjustment(fc)
            if rd.get("indoor"):
                indoor = True
            days.append(DailyPlan(
                date=ds, weekday=weekday,
                rest_day=rd.get("rest_day", True),
                session_type=rd.get("session_type", "rest"),
                target_zone=rd.get("target_zone", "—"),
                duration_min=rd.get("duration_min", 0),
                target_tss=rd.get("target_tss", 0.0),
                indoor=indoor,
                description=rd.get("description", ""),
                weather_note=weather_note or rd.get("weather_note", ""),
                rationale=rd.get("rationale", ""),
                weather_temp_max=fc.get("temp_max", 0),
                weather_temp_min=fc.get("temp_min", 0),
                weather_precip=fc.get("precipitation_prob", 0),
                weather_condition=fc.get("condition", ""),
            ))
        return days

    # Retry loop: up to 3 attempts with validation feedback
    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        prompt = _build_prompt() if attempt == 0 else _build_prompt(
            "VALIDATION ERRORS:\n" + "\n".join(f"- {e.message}" for e in last_errors)
        )

        try:
            response = generate(prompt, stream=False)
        except Exception as e:
            logger.exception(f"AI plan LLM call failed (attempt {attempt+1})")
            if attempt < MAX_RETRIES - 1:
                continue
            return generate_weekly_plan()

        raw_days = _parse_llm_response(response)
        if raw_days is None:
            logger.warning("AI plan: no valid JSON in response, falling back to rules")
            return generate_weekly_plan()

        days = _raw_to_days(raw_days)
        validation = validate_plan(ctx, days)
        last_errors = validation.errors

        if validation.valid:
            # Build weekly plan with projected series
            weekly_tss = sum(d.target_tss for d in days if not d.rest_day)
            daily_tss = [d.target_tss for d in days]
            ctl_s, atl_s, tsb_s = project_tsb(ctx, daily_tss)

            return WeeklyPlan(
                week_start=ctx.week_start.isoformat(),
                days=days,
                weekly_tss_target=round(ctx.current_ctl * 7 / 30, 1),
                weekly_tss_planned=round(weekly_tss, 1),
                generated_at=today.isoformat(),
                readiness_summary=f"AI Plan - Readiness {ctx.readiness_score:.0f}/100, CTL {ctx.current_ctl:.0f}, TSB {ctx.current_tsb:.0f}",
                ctl_series=[round(c, 1) for c in ctl_s],
                atl_series=[round(a, 1) for a in atl_s],
                tsb_series=[round(t, 1) for t in tsb_s],
            )

        logger.info(f"AI plan attempt {attempt+1} failed: {[e.message for e in validation.errors]}")

    # All retries exhausted — fall back to rules
    logger.warning(f"AI plan failed after {MAX_RETRIES} attempts, falling back to rules")
    return generate_weekly_plan()


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
        "ctl_series": plan.ctl_series,
        "atl_series": plan.atl_series,
        "tsb_series": plan.tsb_series,
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
            ctl_series=data.get("ctl_series", []),
            atl_series=data.get("atl_series", []),
            tsb_series=data.get("tsb_series", []),
        )
    except Exception:
        return None