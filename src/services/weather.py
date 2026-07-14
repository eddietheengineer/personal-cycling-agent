"""
Weather service for the weekly training planner.

Fetches 7-day forecasts from the free Open-Meteo API (no API key required)
and resolves user location from config.env or latest Garmin GPS data.
"""

import logging
import os
import sqlite3
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def _map_weather_code(code: int) -> str:
    """Map Open-Meteo WMO weather code to a human-readable condition."""
    if code == 0:
        return "clear"
    if 1 <= code <= 3:
        return "cloudy"
    if 51 <= code <= 65:
        return "rain"
    if 71 <= code <= 77:
        return "snow"
    if 95 <= code <= 99:
        return "storm"
    return "cloudy"


def get_location() -> Optional[tuple[float, float]]:
    """Return (lat, lon) from saved vault location, config.env WEATHER_LAT/LON,
    or auto-detect from the latest Garmin activity GPS route.

    Returns None when neither source is available.
    """
    # 1. Try saved location in vault
    try:
        from src.config.schedule import load_weather_location
        saved = load_weather_location()
        if saved:
            return saved
    except Exception:
        pass

    # 2. Try environment variables set by config.env
    lat_str = os.environ.get("WEATHER_LAT")
    lon_str = os.environ.get("WEATHER_LON")
    if lat_str and lon_str:
        try:
            return float(lat_str), float(lon_str)
        except ValueError:
            logger.warning(
                "Invalid WEATHER_LAT/LON in environment: %r / %r",
                lat_str,
                lon_str,
            )

    # 2. Fallback: auto-detect from latest Garmin activity GPS route
    try:
        from src.config import db_path

        db_file = str(db_path())
        if not os.path.exists(db_file):
            logger.debug("DB file %s not found; cannot auto-detect location", db_file)
            return None

        conn = sqlite3.connect(db_file)
        try:
            # Find the most recent activity that has GPS route data
            row = conn.execute(
                """
                SELECT a.id
                FROM activities a
                WHERE a.id IN (SELECT DISTINCT activity_id FROM activity_routes)
                ORDER BY a.start_date DESC
                LIMIT 1
                """
            ).fetchone()

            if row is None:
                logger.debug("No activities with GPS route data found")
                return None

            activity_id = row[0]

            # Grab the first GPS point from that activity's route
            point = conn.execute(
                """
                SELECT latitude, longitude
                FROM activity_routes
                WHERE activity_id = ?
                ORDER BY sequence ASC
                LIMIT 1
                """,
                (activity_id,),
            ).fetchone()

            if point is None:
                logger.debug("No route points for activity %s", activity_id)
                return None

            return float(point[0]), float(point[1])
        finally:
            conn.close()
    except Exception:
        logger.exception("Failed to auto-detect location from Garmin GPS data")
        return None

    return None


def get_weekly_forecast(lat: float, lon: float) -> list[dict]:
    """Fetch a 7-day weather forecast from the Open-Meteo API.

    Each returned dict has:
        date               (str)  – YYYY-MM-DD
        temp_max           (float) – daily max temperature in °C
        temp_min           (float) – daily min temperature in °C
        precipitation_prob (int)   – max precipitation probability (0-100)
        wind_speed         (float) – max wind speed at 10 m in km/h
        condition          (str)   – 'clear' | 'cloudy' | 'rain' | 'snow' | 'storm'
        morning            (dict)  – weather for 06:00-11:59
        afternoon          (dict)  – weather for 12:00-17:59
        evening            (dict)  – weather for 18:00-21:59

    Each slot dict: {condition, temp, precip, wind}

    Returns an empty list on network errors or malformed responses.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": [
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "wind_speed_10m_max",
            "weathercode",
        ],
        "hourly": [
            "temperature_2m",
            "precipitation_probability",
            "wind_speed_10m",
            "weathercode",
        ],
        "timezone": "auto",
        "forecast_days": 7,
    }

    try:
        resp = requests.get(OPEN_METEO_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("Open-Meteo request failed: %s", exc)
        return []
    except ValueError as exc:
        logger.warning("Invalid JSON from Open-Meteo: %s", exc)
        return []

    daily = data.get("daily", {})
    hourly = data.get("hourly", {})
    dates = daily.get("time", [])
    if not dates:
        logger.warning("Open-Meteo response missing daily data")
        return []

    # Build hourly lookup: date -> {hour: values}
    hour_times = hourly.get("time", [])
    hour_temps = hourly.get("temperature_2m", [])
    hour_precip = hourly.get("precipitation_probability", [])
    hour_wind = hourly.get("wind_speed_10m", [])
    hour_codes = hourly.get("weathercode", [])

    def _slot_for_hours(hours: list[int]) -> dict:
        """Aggregate hourly data for a list of hours into a single slot."""
        if not hours or not hour_times:
            return {"condition": "unknown", "temp": 0, "precip": 0, "wind": 0}
        temps, precips, winds, codes = [], [], [], []
        for i, t in enumerate(hour_times):
            if t in hours and i < len(hour_temps):
                temps.append(hour_temps[i])
                precips.append(hour_precip[i] if i < len(hour_precip) else 0)
                winds.append(hour_wind[i] if i < len(hour_wind) else 0)
                codes.append(hour_codes[i] if i < len(hour_codes) else 3)
        if not temps:
            return {"condition": "unknown", "temp": 0, "precip": 0, "wind": 0}
        # Dominant condition: most common non-clear code, else clear
        code_counts = {}
        for c in codes:
            code_counts[c] = code_counts.get(c, 0) + 1
        dominant = max(code_counts, key=code_counts.get)
        return {
            "condition": _map_weather_code(dominant),
            "temp": round(sum(temps) / len(temps), 1),
            "precip": max(precips),
            "wind": round(max(winds), 1),
        }

    def _hours_for_slot(date_str: str, slot: str) -> list:
        """Return list of datetime strings for a given date and time slot."""
        result = []
        for t in hour_times:
            if not t.startswith(date_str):
                continue
            hour = int(t[11:13])
            if slot == "morning" and 6 <= hour <= 11:
                result.append(t)
            elif slot == "afternoon" and 12 <= hour <= 17:
                result.append(t)
            elif slot == "evening" and 18 <= hour <= 21:
                result.append(t)
        return result

    forecast: list[dict] = []
    for i, date in enumerate(dates):
        try:
            entry = {
                "date": date,
                "temp_max": float(daily["temperature_2m_max"][i]),
                "temp_min": float(daily["temperature_2m_min"][i]),
                "precipitation_prob": int(daily["precipitation_probability_max"][i]),
                "wind_speed": float(daily["wind_speed_10m_max"][i]),
                "condition": _map_weather_code(int(daily["weathercode"][i])),
                "morning": _slot_for_hours(_hours_for_slot(date, "morning")),
                "afternoon": _slot_for_hours(_hours_for_slot(date, "afternoon")),
                "evening": _slot_for_hours(_hours_for_slot(date, "evening")),
            }
            forecast.append(entry)
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("Skipping malformed forecast day %d: %s", i, exc)
            continue

    return forecast
def is_rideable_window(hourly_data: dict, start_hour: int, end_hour: int, min_clear_hours: int = 1) -> tuple[bool, str]:
    """Check if there's a contiguous block of clear-enough hours within a ride window.

    Args:
        hourly_data: dict with morning/afternoon/evening keys, each {condition, temp, precip, wind}.
        start_hour: start of ride window (0-23)
        end_hour: end of ride window (0-23)
        min_clear_hours: minimum contiguous clear hours needed (default 1)

    Returns:
        (rideable: bool, note: str)
    """
    slot_hours = {
        "morning": range(6, 12),
        "afternoon": range(12, 18),
        "evening": range(18, 22),
    }

    hour_weather: dict[int, dict] = {}
    for slot_name, hours in slot_hours.items():
        sd = hourly_data.get(slot_name, {})
        if sd and sd.get("condition"):
            for h in hours:
                hour_weather[h] = sd

    window_hours = list(range(start_hour, end_hour))
    if not window_hours:
        return False, "Empty ride window"

    best_streak = 0
    current_streak = 0
    bad_hours = []

    for h in window_hours:
        hw = hour_weather.get(h, {})
        condition = hw.get("condition", "unknown")
        precip = hw.get("precip", 0)

        if condition in ("storm", "snow") or precip >= 40:
            current_streak = 0
            bad_hours.append(h)
        else:
            current_streak += 1
            best_streak = max(best_streak, current_streak)

    if best_streak >= min_clear_hours:
        if bad_hours:
            bad_labels = [f"{h}:00" for h in bad_hours[:3]]
            return True, f"Rideable ({best_streak}h clear), avoid {', '.join(bad_labels)}"
        return True, f"{best_streak}h clear window available"

    return False, f"Only {best_streak}h clear, need {min_clear_hours}h"