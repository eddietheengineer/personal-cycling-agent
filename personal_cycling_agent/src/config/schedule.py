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
    "monday": {"available": False, "time_slot": "morning"},
    "tuesday": {"available": False, "time_slot": "morning"},
    "wednesday": {"available": False, "time_slot": "morning"},
    "thursday": {"available": False, "time_slot": "morning"},
    "friday": {"available": False, "time_slot": "morning"},
    "saturday": {"available": False, "time_slot": "morning"},
    "sunday": {"available": False, "time_slot": "morning"},
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
            result[day] = {
                "available": bool(entry.get("available", False)),
                "time_slot": entry.get("time_slot", "morning"),
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


def get_time_slot(day: int) -> str:
    """Return the time_slot string for a given weekday int (0=Mon … 6=Sun).

    Returns 'morning' as the default if the day is out of range or
    the schedule has no entry for it.
    """
    if not 0 <= day <= 6:
        return "morning"
    schedule = load_schedule()
    return schedule.get(DAY_NAMES[day], {}).get("time_slot", "morning")