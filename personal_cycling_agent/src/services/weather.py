"""
Weather service for the weekly training planner.

Fetches 7-day forecasts from the free Open-Meteo API (no API key required)
and resolves user location from config.env or latest Garmin GPS data.
"""

import logging
import os
import sqlite3
from pathlib import Path
from urllib.request import urlopen

import json

from src.config.constants import WEATHER_PRECIP_RIDEABLE, HTTP_TIMEOUT_SEC

logger = logging.getLogger(__name__)

SLOT_HOURS = {
    "morning": range(6, 12),
    "afternoon": range(12, 18),
    "evening": range(18, 22),
}


def _hour_weather(hourly_data: dict, hour: int) -> dict:
    """Get weather dict for a specific hour from slot data."""
    for slot_name, hours in SLOT_HOURS.items():
        if hour in hours:
            return hourly_data.get(slot_name, {})
    return {}


def _is_clear(hw: dict) -> bool:
    """Check if a weather dict represents rideable conditions."""
    condition = hw.get("condition", "unknown")
    precip = hw.get("precip", 0)
    return condition not in ("storm", "snow") and precip < WEATHER_PRECIP_RIDEABLE


def find_rideable_slots(forecast_day: dict, available_hours: list[int],
                        min_contiguous: int = 1) -> list[list[int]]:
    """Find all contiguous blocks of clear hours within available hours.

    Returns a list of contiguous hour lists, each of length >= min_contiguous.
    E.g. [[6,7,8,9], [14,15]] means 06:00-09:59 and 14:00-15:59 are rideable.
    """
    if not available_hours or not forecast_day:
        return []

    avail_set = set(available_hours)
    clear_hours = sorted(h for h in available_hours if _is_clear(_hour_weather(forecast_day, h)))

    if not clear_hours:
        return []

    # Group into contiguous blocks
    blocks: list[list[int]] = []
    current: list[int] = [clear_hours[0]]
    for h in clear_hours[1:]:
        if h == current[-1] + 1:
            current.append(h)
        else:
            if len(current) >= min_contiguous:
                blocks.append(current)
            current = [h]
    if len(current) >= min_contiguous:
        blocks.append(current)

    return blocks


def get_location() -> tuple[float, float] | None:
    """Get user location from config or Garmin GPS data."""
    lat = os.environ.get("WEATHER_LAT")
    lon = os.environ.get("WEATHER_LON")
    if lat and lon:
        return float(lat), float(lon)

    from src.config import vault_path
    vp = vault_path()
    if vp:
        loc_file = vp / "weather_location.json"
        if loc_file.exists():
            try:
                data = json.loads(loc_file.read_text())
                return data["lat"], data["lon"]
            except (json.JSONDecodeError, KeyError):
                pass

    return None


def get_weekly_forecast(lat: float, lon: float) -> list[dict]:
    """Fetch a 7-day weather forecast from the Open-Meteo API.

    Returns a list of 7 dicts, one per day, each containing:
    - date: ISO date string
    - temp_max, temp_min: daily temps in F
    - morning, afternoon, evening: {condition, temp, precip, wind}
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum"
        f"&hourly=weather_code,temperature_2m,precipitation_probability,wind_speed_10m"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
        f"&forecast_days=7"
    )
    try:
        with urlopen(url, timeout=HTTP_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.error("Weather fetch failed: %s", e)
        return []

    daily = data.get("daily", {})
    hourly = data.get("hourly", {})
    dates = daily.get("time", [])
    temps_max = daily.get("temperature_2m_max", [])
    temps_min = daily.get("temperature_2m_min", [])
    precip_max = daily.get("precipitation_sum", [])

    h_times = hourly.get("time", [])
    h_codes = hourly.get("weather_code", [])
    h_temps = hourly.get("temperature_2m", [])
    h_precip_prob = hourly.get("precipitation_probability", [])
    h_wind = hourly.get("wind_speed_10m", [])

    # WMO weather code to condition
    code_map = {
        0: "clear", 1: "clear", 2: "partly_cloudy", 3: "cloudy",
        45: "fog", 48: "fog",
        51: "drizzle", 53: "drizzle", 55: "drizzle",
        61: "rain", 63: "rain", 65: "rain",
        66: "freezing_rain", 67: "freezing_rain",
        71: "snow", 73: "snow", 75: "snow",
        77: "snow",
        80: "rain", 81: "rain", 82: "rain",
        85: "snow", 86: "snow",
        95: "storm", 96: "storm", 99: "storm",
    }

    def _hour_idx(hour_str: str) -> int:
        """Find index of hour in hourly time list."""
        for i, t in enumerate(h_times):
            if t.startswith(hour_str[:10]):
                return i
        return -1

    forecast = []
    for i, date in enumerate(dates):
        day_data = {
            "date": date,
            "temp_max": temps_max[i] if i < len(temps_max) else 0,
            "temp_min": temps_min[i] if i < len(temps_min) else 0,
            "precip_max": precip_max[i] if i < len(precip_max) else 0,
        }

        for slot_name, hours in SLOT_HOURS.items():
            slot_temps = []
            slot_precips = []
            slot_winds = []
            slot_codes = []

            for h in hours:
                h_str = f"{date}T{h:02d}:00"
                idx = _hour_idx(h_str)
                if idx >= 0 and idx < len(h_times):
                    slot_temps.append(h_temps[idx] if idx < len(h_temps) else 0)
                    slot_precips.append(h_precip_prob[idx] if idx < len(h_precip_prob) else 0)
                    slot_winds.append(h_wind[idx] if idx < len(h_wind) else 0)
                    code = h_codes[idx] if idx < len(h_codes) else 3
                    slot_codes.append(code)

            if slot_temps:
                avg_temp = sum(slot_temps) / len(slot_temps)
                avg_precip = sum(slot_precips) / len(slot_precips)
                avg_wind = sum(slot_winds) / len(slot_winds)
                # Use worst code in the slot
                worst_code = max(slot_codes, key=lambda c: code_map.get(c, "unknown") in ("storm", "rain", "snow"))
                condition = code_map.get(worst_code, "unknown")
            else:
                avg_temp = 0
                avg_precip = 0
                avg_wind = 0
                condition = "unknown"

            day_data[slot_name] = {
                "condition": condition,
                "temp": round(avg_temp),
                "precip": round(avg_precip),
                "wind": round(avg_wind),
            }

        forecast.append(day_data)
    return forecast


def find_ride_slot(forecast_day: dict, available_hours: list[int],
                   ride_duration_hours: float = 1.5) -> tuple[int | None, str]:
    """Find the best contiguous ride slot within available hours.

    Strategy:
    - For each possible ride start hour in available_hours:
      - Check if ride_duration_hours of contiguous clear hours exist from start
      - Check 1h buffer before start (if in available_hours) is clear
      - Check 1h buffer after ride end (if in available_hours) is clear
    - Returns (best_start_hour, note) or (None, note)

    Args:
        forecast_day: single day forecast dict with morning/afternoon/evening keys
        available_hours: sorted list of hours (0-23) the user is available
        ride_duration_hours: ride duration in hours (default 1.5)

    Returns:
        (start_hour_or_None, descriptive_note)
    """
    if not available_hours:
        return None, "No available hours"

    ride_slots_needed = int(ride_duration_hours)
    if ride_slots_needed < 1:
        ride_slots_needed = 1

    best_start = None
    best_score = -1
    best_note = ""

    for start_idx, start_hour in enumerate(available_hours):
        # Check if we have enough contiguous available hours for the ride
        ride_hours = []
        for h in range(start_hour, start_hour + ride_slots_needed):
            if h in available_hours:
                ride_hours.append(h)
            else:
                break

        if len(ride_hours) < ride_slots_needed:
            continue

        # Score this slot
        score = 0
        ride_clear = True
        ride_bad = []

        for h in ride_hours:
            hw = _hour_weather(forecast_day, h)
            if _is_clear(hw):
                score += 2
            else:
                ride_clear = False
                ride_bad.append(h)

        # Buffer before (1 hour before ride start, if in available_hours)
        buffer_before = start_hour - 1
        if buffer_before in available_hours:
            hw = _hour_weather(forecast_day, buffer_before)
            if not _is_clear(hw):
                score -= 5  # Heavy penalty: rain right before ride

        # Buffer after (1 hour after ride ends, if in available_hours)
        buffer_after = ride_hours[-1] + 1
        if buffer_after in available_hours:
            hw = _hour_weather(forecast_day, buffer_after)
            if not _is_clear(hw):
                score -= 5  # Heavy penalty: rain right after ride

        # Only consider slots where the ride hours themselves are clear
        if not ride_clear:
            continue

        if score > best_score:
            best_score = score
            best_start = start_hour
            end_hour = ride_hours[-1] + 1
            note_parts = [f"Ride {start_hour:02d}:00-{end_hour:02d}:00 ✓"]
            if buffer_before in available_hours:
                hw = _hour_weather(forecast_day, buffer_before)
                if not _is_clear(hw):
                    note_parts.append(f"rain at {buffer_before:02d}:00 (before)")
            if buffer_after in available_hours:
                hw = _hour_weather(forecast_day, buffer_after)
                if not _is_clear(hw):
                    note_parts.append(f"rain at {buffer_after:02d}:00 (after)")
            best_note = " ".join(note_parts)

    if best_start is not None:
        return best_start, best_note

    # No good slot found — explain why
    total_clear = 0
    for h in available_hours:
        hw = _hour_weather(forecast_day, h)
        if _is_clear(hw):
            total_clear += 1

    if total_clear == 0:
        return None, f"All {len(available_hours)} available hours have bad weather"
    return None, f"Only {total_clear}/{len(available_hours)} available hours clear, need {ride_slots_needed} contiguous"