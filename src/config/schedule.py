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
    """Return the path to the training schedule JSON file in the vault."""
    return vault_path() / "training_schedule.json"


def _migrate_entry(entry: dict) -> dict:
    """Migrate legacy time_slots to ride_windows if needed."""
    if "ride_windows" in entry:
        return entry
    # Legacy: time_slots -> ride_windows
    slots = entry.get("time_slots", ["morning"])
    if isinstance(slots, str):
        slots = [slots]
    windows = [_SLOT_TO_WINDOW.get(s, {"start": 6, "end": 12}) for s in slots]
    return {
        "available": bool(entry.get("available", False)),
        "ride_windows": windows,
    }


def load_schedule() -> dict:
    """Load the training schedule from the vault.

    Returns DEFAULT_SCHEDULE if the file does not exist or is invalid.
    Auto-migrates legacy time_slots to ride_windows.
    """
    path = _schedule_file()
    if not path.exists():
        return dict(DEFAULT_SCHEDULE)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        result = {}
        for day in DAY_NAMES:
            entry = data.get(day, {})
            result[day] = _migrate_entry(entry)
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


def get_ride_windows(day: int) -> list[dict]:
    """Return ride_windows list for a given weekday int (0=Mon … 6=Sun).

    Each window: {"start": int, "end": int} (hours 0-23).
    Returns [{"start": 6, "end": 12}] as default.
    """
    if not 0 <= day <= 6:
        return [{"start": 6, "end": 12}]
    schedule = load_schedule()
    return schedule.get(DAY_NAMES[day], {}).get("ride_windows", [{"start": 6, "end": 12}])
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