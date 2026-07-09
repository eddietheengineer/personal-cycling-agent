"""
Intervals.icu API client for cycling-ai-agent.

Authenticates via personal API key (basic auth) and fetches:
- Wellness data (HRV/RMSSD, RHR, weight, stress, sleep) for the last 90 days.
- Completed activity summaries for the last 90 days.

All data is stored to a local SQLite database via the db module.
"""

import gzip
import io
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import requests

from src import config

config.setup()

logger = logging.getLogger(__name__)

BASE_URL = os.getenv("INTERVALS_ICU_BASE_URL", "https://intervals.icu")
API_KEY = os.getenv("INTERVALS_ICU_API_KEY", "")
API_SECRET = os.getenv("INTERVALS_ICU_API_SECRET", "")
ATHLETE_ID = os.getenv("INTERVALS_ICU_ATHLETE_ID", "0")  # "0" = self

DEFAULT_LOOKBACK_DAYS = 90


def _headers() -> dict[str, str]:
    """Return headers with basic auth for personal API key."""
    return {"Accept": "application/json"}


def _session() -> requests.Session:
    """Create a requests session authenticated with the API key/secret."""
    session = requests.Session()
    if API_KEY and API_SECRET:
        session.auth = (API_KEY, API_SECRET)
    session.headers.update(_headers())
    return session


def fetch_wellness(
    oldest: str | None = None,
    newest: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch wellness records from Intervals.icu.

    Args:
        oldest: ISO-8601 date string (YYYY-MM-DD). Defaults to 90 days ago.
        newest: ISO-8601 date string. Defaults to today.

    Returns:
        List of wellness record dicts. Each record has keys like:
        - id (date string), weight, resting_hr, rmssd, stress, sleep_score, etc.
    """
    if not oldest:
        oldest = (datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if not newest:
        newest = datetime.now().strftime("%Y-%m-%d")

    url = f"{BASE_URL}/api/v1/athlete/{ATHLETE_ID}/wellness"
    params = {"oldest": oldest, "newest": newest}

    logger.info(f"Fetching wellness data: {oldest} to {newest}")

    with _session() as session:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, list):
        logger.warning(f"Unexpected wellness response type: {type(data)}")
        return []

    logger.info(f"Fetched {len(data)} wellness records")
    return data


def fetch_activities(
    oldest: str | None = None,
    newest: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch completed activity summaries from Intervals.icu.

    Args:
        oldest: ISO-8601 date string. Defaults to 90 days ago.
        newest: ISO-8601 date string. Defaults to today.

    Returns:
        List of activity summary dicts. Each has keys like:
        - id, start_date_local, type, duration, distance, average_power,
          max_power, average_hr, max_hr, calories, etc.
    """
    if not oldest:
        oldest = (datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    if not newest:
        newest = datetime.now().strftime("%Y-%m-%d")

    url = f"{BASE_URL}/api/v1/athlete/{ATHLETE_ID}/activities"
    params = {"oldest": oldest, "newest": newest}

    logger.info(f"Fetching activities: {oldest} to {newest}")

    with _session() as session:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

    if not isinstance(data, list):
        logger.warning(f"Unexpected activities response type: {type(data)}")
        return []

    logger.info(f"Fetched {len(data)} activity summaries")
    return data


def download_activity_fit_file(activity_id: str, save_raw: bool = True) -> bytes:
    """
    Download the Intervals.icu generated FIT file for an activity.

    The file is gzip-compressed; this returns the decompressed bytes.
    If save_raw is True, the file is also saved to the vault's raw/ directory.

    Args:
        activity_id: Intervals.icu activity ID (e.g. "i55751783").
        save_raw: If True, save the raw FIT file to the vault.

    Returns:
        Raw FIT file bytes.
    """
    url = f"{BASE_URL}/api/v1/activity/{activity_id}/fit-file"

    logger.info(f"Downloading FIT file for activity {activity_id}")

    with _session() as session:
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        compressed = resp.content

    decompressed = gzip.decompress(compressed)

    if save_raw:
        from src import config
        raw_dir = config.raw_dir()
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / f"{activity_id}.fit"
        with open(path, "wb") as f:
            f.write(decompressed)
        logger.info(f"Saved raw FIT file to {path}")

    logger.info(f"Downloaded {len(decompressed)} bytes FIT data")
    return decompressed


def download_activity_raw_file(activity_id: str, save_raw: bool = True) -> bytes:
    """
    Download the original activity file (FIT/TCX/GPX) for an activity.

    If save_raw is True, the file is also saved to the vault's raw/ directory.

    Returns decompressed bytes.
    """
    url = f"{BASE_URL}/api/v1/activity/{activity_id}/file"

    logger.info(f"Downloading raw file for activity {activity_id}")

    with _session() as session:
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        compressed = resp.content

    decompressed = gzip.decompress(compressed)

    if save_raw:
        from src import config
        raw_dir = config.raw_dir()
        raw_dir.mkdir(parents=True, exist_ok=True)
        # Detect file type from Intervals.icu response if available, default to .fit
        path = raw_dir / f"{activity_id}.fit"
        with open(path, "wb") as f:
            f.write(decompressed)
        logger.info(f"Saved raw file to {path}")

    return decompressed


def ingest_all(db_path: str | None = None) -> dict[str, int]:
    """
    Fetch wellness and activities, then persist to SQLite.

    Returns a dict with counts of inserted/updated records.
    """
    if db_path is None:
        from src import config
        db_path = str(config.db_path("cycling_agent.sqlite"))

    from src.db.store import CyclingDB

    wellness = fetch_wellness()
    activities = fetch_activities()

    db = CyclingDB(db_path)
    db.store_wellness(wellness)
    db.store_activities(activities)
    db.close()

    counts = {
        "wellness_records": len(wellness),
        "activity_records": len(activities),
    }
    logger.info(f"Ingestion complete: {counts}")
    return counts


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if not API_KEY:
        logger.error("INTERVALS_ICU_API_KEY not set in .env")
        raise SystemExit(1)

    counts = ingest_all()
    print(f"Done. Wellness: {counts['wellness_records']}, Activities: {counts['activity_records']}")