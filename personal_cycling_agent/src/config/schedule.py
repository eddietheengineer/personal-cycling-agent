"""
Training schedule configuration.

Stores which days of the week are available for training and the preferred
time slot for each day.  Persisted as JSON in the vault so it survives
restarts but stays out of version control.
"""

import json
from pathlib import Path

from src.config import vault_path

DAY_NAMES = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

DEFAULT_SCHEDULE: dict[str, dict[str, object]] = {
    "monday": {"available": False, "ride_windows": [{"start": 6, "end": 12}]},
    "tuesday": {"available": False, "ride_windows": [{"start": 6, "end": 12}]},
    "wednesday": {"available": False, "ride_windows": [{"start": 6, "end": 12}]},
    "thursday": {"available": False, "ride_windows": [{"start": 6, "end": 12}]},
    "friday": {"available": False, "ride_windows": [{"start": 6, "end": 12}]},
    "saturday": {"available": False, "ride_windows": [{"start": 6, "end": 12}]},
    "sunday": {"available": False, "ride_windows": [{"start": 6, "end": 12}]},
}

# Map legacy time_slot strings to hour ranges
_SLOT_TO_WINDOW: dict[str, dict[str, int]] = {
    "morning": {"start": 6, "end": 12},
    "afternoon": {"start": 12, "end": 18},
    "evening": {"start": 17, "end": 22},
    "any": {"start": 6, "end": 22},
}


def _schedule_file() -> Path:
    return vault_path() / "training_schedule.json"


def _migrate_entry(entry: dict) -> dict:
    """Migrate any legacy format to available_hours."""
    if "available_hours" in entry:
        return {"available_hours": list(entry["available_hours"])}
    if "ride_windows" in entry:
        hours = set()
        for w in entry["ride_windows"]:
            for h in range(w.get("start", 6), w.get("end", 12)):
                hours.add(h)
        return {"available_hours": sorted(hours)}
    slot_map = {"morning": range(6, 12), "afternoon": range(12, 18),
                "evening": range(17, 22), "any": range(6, 22)}
    slots = entry.get("time_slots", [])
    if isinstance(slots, str):
        slots = [slots]
    hours = set()
    for s in slots:
        hours.update(slot_map.get(s, range(6, 12)))
    return {"available_hours": sorted(hours)}


def load_schedule() -> dict:
    path = _schedule_file()
    if not path.exists():
        return dict(DEFAULT_SCHEDULE)
    try:
        with open(path) as f:
            data = json.load(f)
        return {day: _migrate_entry(data.get(day, {})) for day in DAY_NAMES}
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SCHEDULE)


def save_schedule(schedule: dict) -> None:
    path = _schedule_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(schedule, f, indent=2)


def get_available_days() -> list[int]:
    schedule = load_schedule()
    return [i for i, day in enumerate(DAY_NAMES)
            if schedule.get(day, {}).get("available_hours", [])]


def get_available_hours(day: int) -> list[int]:
    """Return list of available hours (0-23) for a weekday."""
    if not 0 <= day <= 6:
        return []
    schedule = load_schedule()
    return schedule.get(DAY_NAMES[day], {}).get("available_hours", [])
def _weather_file() -> Path:
    """Return the path to the weather location JSON file in the vault."""
    return vault_path() / "weather_location.json"


def load_weather_location() -> tuple[float, float] | None:
    """Load saved weather location (lat, lon) from the vault."""
    path = _weather_file()
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        lat = float(data.get("lat"))
        lon = float(data.get("lon"))
        return lat, lon
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return None


def save_weather_location(lat: float, lon: float) -> None:
    """Persist weather location to the vault."""
    path = _weather_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({"lat": lat, "lon": lon}, f, indent=2)