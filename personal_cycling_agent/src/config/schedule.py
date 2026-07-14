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
    "monday": {"available": False, "time_slots": ["morning"]},
    "tuesday": {"available": False, "time_slots": ["morning"]},
    "wednesday": {"available": False, "time_slots": ["morning"]},
    "thursday": {"available": False, "time_slots": ["morning"]},
    "friday": {"available": False, "time_slots": ["morning"]},
    "saturday": {"available": False, "time_slots": ["morning"]},
    "sunday": {"available": False, "time_slots": ["morning"]},
}


def _schedule_file() -> Path:
    """Return the path to the training schedule JSON file in the vault."""
    return vault_path() / "training_schedule.json"


def load_schedule() -> dict:
    """Load the training schedule from the vault.

    Returns DEFAULT_SCHEDULE if the file does not exist or is invalid.
    """
    path = _schedule_file()
    if not path.exists():
        return dict(DEFAULT_SCHEDULE)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        # Validate structure — fall back to defaults for missing keys
        result = {}
        for day in DAY_NAMES:
            entry = data.get(day, {})
            slots = entry.get("time_slots", ["morning"])
            if isinstance(slots, str):
                slots = [slots]  # backward compat: single string -> list
            result[day] = {
                "available": bool(entry.get("available", False)),
                "time_slots": slots,
            }
        return result
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SCHEDULE)


def save_schedule(schedule: dict) -> None:
    """Persist the training schedule to the vault."""
    path = _schedule_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(schedule, f, indent=2)


def get_available_days() -> list[int]:
    """Return weekday ints (0=Mon … 6=Sun) where available=True."""
    schedule = load_schedule()
    return [
        i
        for i, day in enumerate(DAY_NAMES)
        if schedule.get(day, {}).get("available", False)
    ]


def get_time_slots(day: int) -> list[str]:
    """Return the time_slots list for a given weekday int (0=Mon … 6=Sun).

    Returns ['morning'] as the default if the day is out of range or
    the schedule has no entry for it.
    """
    if not 0 <= day <= 6:
        return ["morning"]
    schedule = load_schedule()
    return schedule.get(DAY_NAMES[day], {}).get("time_slots", ["morning"])
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