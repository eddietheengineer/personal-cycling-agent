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
    dates = daily.get("temperature_2m_max", [])
    if not dates:
        logger.warning("Open-Meteo response missing daily data")
        return []

    forecast: list[dict] = []
    for i, date in enumerate(dates):
        try:
            forecast.append(
                {
                    "date": date,
                    "temp_max": float(daily["temperature_2m_max"][i]),
                    "temp_min": float(daily["temperature_2m_min"][i]),
                    "precipitation_prob": int(
                        daily["precipitation_probability_max"][i]
                    ),
                    "wind_speed": float(daily["wind_speed_10m_max"][i]),
                    "condition": _map_weather_code(
                        int(daily["weathercode"][i])
                    ),
                }
            )
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("Skipping malformed forecast day %d: %s", i, exc)
            continue

    return forecast