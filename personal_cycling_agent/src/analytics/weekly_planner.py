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

    # Score each available day using find_ride_slot
    ride_duration_hours = _parse_float(profile.get("max_session_duration"), 90.0) / 60.0
    day_scores: list[tuple[int, date, float, dict | None, list[int], str]] = []
    for i, day_date in enumerate(week_dates):
        weekday = day_date.weekday()
        date_str = day_date.isoformat()
        if weekday not in available_days:
            continue
        avail_hours = get_available_hours(weekday)
        day_forecast = forecast_map.get(date_str)

        score = 0.0
        ride_note = ""
        if day_forecast and avail_hours:
            slot_start, ride_note = find_ride_slot(day_forecast, avail_hours, ride_duration_hours)
            if slot_start is not None:
                score = 100.0
            else:
                score = -1.0  # Not rideable

        day_scores.append((i, day_date, score, day_forecast, avail_hours, ride_note))

    MAX_TRAINING_DAYS = 3
    # Sort by score descending, pick top MAX_TRAINING_DAYS
    day_scores.sort(key=lambda x: -x[2])
    training_days = set()
    for entry in day_scores:
        if len(training_days) >= MAX_TRAINING_DAYS:
            break
        # Skip days with no rideable slot (score < 0) unless we have no other choice
        if entry[2] < 0 and len(training_days) < len(day_scores):
            continue
        training_days.add(entry[0])

    # If we skipped too many due to weather, fill remaining slots
    if len(training_days) < MAX_TRAINING_DAYS:
        for entry in day_scores:
            if len(training_days) >= MAX_TRAINING_DAYS:
                break
            if entry[0] not in training_days:
                training_days.add(entry[0])

    # Build a map of day_idx -> ride_note for later use
    ride_notes = {x[0]: x[5] for x in day_scores}

    # Build daily plans
    days: list[DailyPlan] = []
    training_day_counter = 0
    weekly_tss = 0.0

    # Session pattern for 3-day week
    session_pattern = ["endurance", "threshold", "vo2"]

    for i, day_date in enumerate(week_dates):
        weekday = day_date.weekday()
        date_str = day_date.isoformat()
        avail_hours = get_available_hours(weekday)
        day_forecast = forecast_map.get(date_str)
        indoor, weather_note = _weather_adjustment(day_forecast)

        if i not in training_days:
            w_temp_max = day_forecast.get("temp_max", 0) if day_forecast else 0
            w_temp_min = day_forecast.get("temp_min", 0) if day_forecast else 0
            w_precip = day_forecast.get("precipitation_prob", 0) if day_forecast else 0
            w_condition = day_forecast.get("condition", "") if day_forecast else ""
            ride_note = ride_notes.get(i, "")
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
                weather_temp_max=w_temp_max,
                weather_temp_min=w_temp_min,
                weather_precip=w_precip,
                weather_condition=w_condition,
                ride_note=ride_note,
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

        window_label = f"{avail_hours[0]:02d}:00-{avail_hours[-1]+1:02d}:00" if avail_hours else "morning"
        description = _build_description(session_type, zone, duration, tss, indoor, window_label)

        w_temp_max = day_forecast.get("temp_max", 0) if day_forecast else 0
        w_temp_min = day_forecast.get("temp_min", 0) if day_forecast else 0
        w_precip = day_forecast.get("precipitation_prob", 0) if day_forecast else 0
        w_condition = day_forecast.get("condition", "") if day_forecast else ""
        rationale_parts = []
        if day_date == today:
            rationale_parts.append(f"Today's readiness: {readiness_score:.0f}/100")
        rationale_parts.append(f"Projected TSB: {proj_tsb:.0f}")
        if weather_note:
            rationale_parts.append(weather_note)
        rationale_parts.append("Selected as one of 3 best weather days")

        ride_note = ride_notes.get(i, "")
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
            weather_temp_max=w_temp_max,
            weather_temp_min=w_temp_min,
            weather_precip=w_precip,
            weather_condition=w_condition,
            ride_note=ride_note,
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

    # Project CTL/ATL/TSB across the week
    daily_tss = [d.target_tss for d in days]
    ctl_proj, atl_proj = _project_ctl_atl(current_ctl, current_atl, daily_tss)
    tsb_proj = [ctl - atl for ctl, atl in zip(ctl_proj, atl_proj)]

    plan = WeeklyPlan(
        week_start=week_start.isoformat(),
        days=days,
        weekly_tss_target=round(weekly_tss_target, 1),
        weekly_tss_planned=round(weekly_tss, 1),
        generated_at=today.isoformat() + "T" + "__TIME__",
        readiness_summary=f"Readiness {readiness_score:.0f}/100, CTL {current_ctl:.0f}, ATL {current_atl:.0f}, TSB {current_tsb:.0f}",
        ctl_series=[round(c, 1) for c in ctl_proj],
        atl_series=[round(a, 1) for a in atl_proj],
        tsb_series=[round(t, 1) for t in tsb_proj],
    )

    return plan
def generate_ai_plan() -> WeeklyPlan:
    """Generate a weekly plan using LLM analysis of recent history and readiness."""
    from src.agent.llm_client import generate

    today = date.today()
    week_start = today
    week_dates = [today + timedelta(days=i) for i in range(7)]

    analysis = _load_analysis()
    profile = _load_profile()
    available_days = get_available_days()

    training_load = analysis.get("training_load", {})
    current_ctl = _parse_float(training_load.get("ctl"), 100.0)
    current_atl = _parse_float(training_load.get("atl"), 80.0)
    current_tsb = current_ctl - current_atl

    readiness = analysis.get("readiness", {})
    readiness_score = _parse_float(readiness.get("composite_score"), 70.0)
    readiness_rec = readiness.get("recommendation", "")
    readiness_state = readiness.get("state", "")
    cp = _parse_float(analysis.get("cp"), 224.0)

    forecast_map: dict[str, dict] = {}
    location = get_location()
    if location:
        forecasts = get_weekly_forecast(location[0], location[1])
        for f in forecasts:
            forecast_map[f.get("date", "")] = f

    weather_lines = []
    ride_duration = _parse_float(profile.get("max_session_duration"), 90.0) / 60.0
    for d in week_dates:
        ds = d.isoformat()
        fc = forecast_map.get(ds, {})
        weekday = d.weekday()
        tmax_f = fc.get("temp_max", 0)
        tmin_f = fc.get("temp_min", 0)
        slots_info = []
        for slot_name in ["morning", "afternoon", "evening"]:
            sd = fc.get(slot_name, {})
            if sd and sd.get("condition"):
                st_f = sd.get("temp", 0)
                slots_info.append(f"{slot_name}: {sd['condition']} {st_f:.0f}F precip {sd.get('precip', 0)}%")
        slot_str = " | ".join(slots_info) if slots_info else ""

        # Ride slot analysis
        avail_hours = get_available_hours(weekday)
        ride_slot_note = ""
        if avail_hours and fc:
            from src.services.weather import find_ride_slot
            slot_start, slot_note = find_ride_slot(fc, avail_hours, ride_duration)
            if slot_start is not None:
                ride_slot_note = f" [RIDEABLE: {slot_note}]"
            else:
                ride_slot_note = f" [NOT RIDEABLE: {slot_note}]"

        weather_lines.append(
            f"{ds}: {fc.get('condition','unknown')} {tmax_f:.0f}F/{tmin_f:.0f}F precip {fc.get('precipitation_prob',0)}% {slot_str}{ride_slot_note}"
        )

    day_names = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    available_days_str = ", ".join(day_names[i] for i in available_days)

    # Load recent memory journal entries for context
    journal_context = ""
    try:
        from src.memory.journal import load_recent
        journal_context = load_recent(30)
    except Exception:
        pass

    weather_block = "WEATHER:\n" + "\n".join(weather_lines) + "\n\n"

    prompt = (
        f"You are a cycling coach. Generate a 7-day training plan.\n\n"
        f"ATHLETE: Readiness {readiness_score:.0f}/100 ({readiness_state}), "
        f"CTL {current_ctl:.0f}, ATL {current_atl:.0f}, TSB {current_tsb:.0f}, "
        f"CP {cp:.0f}W\n"
        f"Recommendation: {readiness_rec}\n"
        f"Goals: {profile.get('primary_goal', 'VO2 max')}\n"
        f"Max session: {_parse_float(profile.get('max_session_duration'), 90.0):.0f}min\n"
        f"Constraints: {profile.get('available_training_days', 'None')}\n\n"
        f"## HARD CONSTRAINTS:\n"
        f"1. Pick 2-3 training days (no more than 3). All other days must be rest.\n"
        f"2. Available days: {available_days_str}. Only pick from these.\n"
        f"3. PICK THE BEST WEATHER DAYS. Each day has a [RIDEABLE] or [NOT RIDEABLE] tag.\n"
        f"   [RIDEABLE] means there's a contiguous clear window for a {_parse_float(profile.get('max_session_duration'), 90.0):.0f}-min ride\n"
        f"   with 1h buffer before/after. Prefer [RIDEABLE] days.\n"
        f"   [NOT RIDEABLE] means weather blocks a clean ride window — set indoor=true if you pick it.\n"
        f"4. If readiness < 60, use only recovery or endurance. No threshold/VO2.\n"
        f"5. You may do 2 longer rides or 3 shorter ones — use your judgment.\n\n"
        + weather_block
        + journal_context
        + f"Return ONLY a JSON array of 7 day objects:\n"
        + f'[{{"date":"YYYY-MM-DD","weekday":0-6,"rest_day":bool,"session_type":"rest|recovery|endurance|threshold|vo2|anaerobic|mixed",'
        + f'"target_zone":"Z1-Z5","duration_min":int,"target_tss":float,"indoor":bool,'
        + f'"description":"str","weather_note":"str","rationale":"str"}}]\n'
        + f"Rest days: rest_day=true, duration=0, tss=0. Total TSS ~{current_ctl*7/30:.0f}."
    )

    try:
        response = generate(prompt, stream=False)
        import re
        json_match = re.search(r'\[[\s\S]*\]', response)
        if not json_match:
            logger.warning("AI plan: no JSON in response, falling back to rules")
            return generate_weekly_plan()

        raw_days = json.loads(json_match.group())
        days = []
        # Map weekday -> correct date for this week
        weekday_to_date = {d.weekday(): d.isoformat() for d in week_dates}

        for rd in raw_days[:7]:
            ds = rd.get("date", "")
            weekday = rd.get("weekday", 0)
            # Force date to match this week's schedule
            ds = weekday_to_date.get(weekday, ds)
            fc = forecast_map.get(ds, {})
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
        # Validate: must have 1-3 training days
        train_count = sum(1 for d in days if not d.rest_day)
        if train_count < 1 or train_count > 3:
            logger.warning(f"AI plan has {train_count} training days (expected 1-3), falling back to rules")
            return generate_weekly_plan()

        weekly_tss = sum(d.target_tss for d in days if not d.rest_day)
        return WeeklyPlan(
            week_start=week_start.isoformat(),
            days=days,
            weekly_tss_target=round(current_ctl * 7 / 30, 1),
            weekly_tss_planned=round(weekly_tss, 1),
            generated_at=today.isoformat(),
            readiness_summary=f"AI Plan - Readiness {readiness_score:.0f}/100, CTL {current_ctl:.0f}, TSB {current_tsb:.0f}",
        )
    except Exception as e:
        logger.exception("AI plan generation failed")
        raise RuntimeError(f"AI plan failed: {e}") from e


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