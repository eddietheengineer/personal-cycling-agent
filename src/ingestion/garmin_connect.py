"""
Garmin Connect API client using python-garminconnect.

Pulls daily wellness data (HRV/RMSSD, RHR, stress, sleep, steps, weight)
directly from Garmin Connect and stores it in the local SQLite database.

This is the primary source for HRV data — Garmin's data export does not
include HRV, so the API is needed for the readiness engine.

Usage:
    from src.ingestion.garmin_connect import sync_garmin
    sync_garmin(days=90)
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any

from src import config
from src.db.store import CyclingDB

logger = logging.getLogger(__name__)

try:
    import garminconnect
except ImportError:
    garminconnect = None  # type: ignore


def _get_garmin_credentials() -> tuple[str, str, str]:
    """
    Get Garmin Connect credentials from the vault.

    Returns (email, password, tokenstore_path).
    The password is stored as a hash in config.env but resolved to plaintext
    by config.setup() before this is called.
    """
    email = os.getenv("GARMIN_EMAIL", "")
    password = os.getenv("GARMIN_PASSWORD", "")
    tokenstore = os.getenv("GARMIN_TOKENSTORE", "")
    return email, password, tokenstore


def _create_client(tokenstore: str) -> "garminconnect.Garmin":
    """Create and return an authenticated Garmin client."""
    if garminconnect is None:
        raise ImportError(
            "garminconnect package not installed. "
            "Run: pip install garminconnect curl_cffi"
        )

    email, password, _ = _get_garmin_credentials()

    client = garminconnect.Garmin(email, password)

    # Use custom token store if specified
    if tokenstore:
        os.makedirs(os.path.dirname(tokenstore) or ".", exist_ok=True)
        client.set_tokenfile(tokenstore)

    return client


def _login(client: "garminconnect.Garmin") -> None:
    """
    Login to Garmin Connect. Handles MFA via callback.

    On first run, Garmin may require MFA. The library will prompt via
    the prompt_mfa callback. Subsequent runs use cached tokens.
    """
    def prompt_mfa():
        code = input("  Enter Garmin MFA code: ").strip()
        return code

    try:
        # Try to use cached tokens first
        client.login(prompt_mfa=prompt_mfa)
    except Exception as e:
        logger.error(f"Garmin login failed: {e}")
        raise


def fetch_wellness_for_date(
    client: "garminconnect.Garmin", date_str: str
) -> dict[str, Any] | None:
    """
    Fetch all wellness data for a single date from Garmin Connect.

    Returns a dict compatible with the wellness table schema, or None
    if no data is available for that date.
    """
    try:
        # Get basic stats (RHR, steps, sleep, stress)
        stats = client.get_stats(date_str)

        # Get HRV data
        hrv_data = client.get_hrv_data(date_str)

        # Get heart rates (for RHR verification)
        heart_rates = client.get_heart_rates(date_str)

        # Get body composition (for weight)
        try:
            body = client.get_body_composition(date_str)
        except Exception:
            body = None

        # Get weigh-ins (more reliable for weight)
        try:
            weigh_ins = client.get_daily_weigh_ins(date_str)
        except Exception:
            weigh_ins = None

        # Extract RHR
        resting_hr = None
        if heart_rates and "restingHeartRateValue" in heart_rates:
            resting_hr = heart_rates["restingHeartRateValue"]
        elif stats and "restingHeartRate" in stats:
            resting_hr = stats["restingHeartRate"]

        # Extract RMSSD from HRV data
        rmssd = None
        if hrv_data and "hrvSummary" in hrv_data:
            summary = hrv_data["hrvSummary"]
            # Garmin returns overnightHRVValue as the primary RMSSD
            rmssd = summary.get("overnightHRVValue")

        # Extract stress
        stress = None
        if stats and "allDayStress" in stats:
            stress = stats["allDayStress"].get("averageStressLevel")

        # Extract steps
        steps = stats.get("totalSteps") if stats else None

        # Extract weight
        weight = None
        if weigh_ins and weigh_ins.get("dailyWeightList"):
            # Use the first weigh-in of the day
            weight = weigh_ins["dailyWeightList"][0].get("weightGrams")
            if weight:
                weight = weight / 1000.0  # convert grams to kg
        elif body:
            weight = body.get("weight")

        # Extract sleep data
        sleep_score = None
        sleep_hours = None
        try:
            sleep_data = client.get_sleep_data(date_str)
            if sleep_data:
                sleep_score = sleep_data.get("sleepScore")
                # Sleep duration in milliseconds -> hours
                sleep_ms = sleep_data.get("sleepTimeSeconds", 0)
                if sleep_ms:
                    sleep_hours = sleep_ms / 3600.0
        except Exception:
            pass

        if not any([resting_hr, rmssd, stress, steps, weight]):
            return None

        return {
            "date": date_str,
            "weight": weight,
            "resting_hr": resting_hr,
            "rmssd": rmssd,
            "stress": stress,
            "sleep_score": sleep_score,
            "sleep_hours": sleep_hours,
            "steps": steps,
        }

    except Exception as e:
        logger.debug(f"Failed to fetch wellness for {date_str}: {e}")
        return None


def sync_garmin(
    days: int = 90,
    db_path: str | None = None,
    tokenstore: str | None = None,
) -> dict[str, int]:
    """
    Sync wellness data from Garmin Connect for the last N days.

    Args:
        days: Number of days to sync back from today.
        db_path: Optional override for the database path.
        tokenstore: Optional path for Garmin auth token cache.

    Returns:
        Dict with counts of new/updated records.
    """
    vault = config.vault_path()
    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    if tokenstore is None:
        tokenstore = str(vault / "garmin_tokens.json")

    email, password, _ = _get_garmin_credentials()
    if not email or not password:
        logger.error(
            "Garmin credentials not set in config.env. "
            "Run 'python setup.py' and set GARMIN_EMAIL and GARMIN_PASSWORD."
        )
        raise SystemExit(1)

    logger.info(f"Syncing Garmin Connect data for last {days} days...")

    # Create client and login
    client = _create_client(tokenstore)
    _login(client)
    logger.info("Authenticated with Garmin Connect")

    # Fetch wellness data for each day
    records = []
    today = datetime.now().date()

    for i in range(days):
        date = today - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")

        record = fetch_wellness_for_date(client, date_str)
        if record:
            records.append(record)

        # Progress indicator every 30 days
        if (i + 1) % 30 == 0:
            logger.info(f"  Processed {i + 1}/{days} days, {len(records)} with data")

    if not records:
        logger.warning("No wellness data retrieved from Garmin Connect")
        return {"wellness_records": 0, "with_hrv": 0}

    # Store in database
    db = CyclingDB(db_path)
    stored = db.store_wellness(records)
    db.close()

    # Count records with HRV
    with_hrv = sum(1 for r in records if r.get("rmssd") is not None)

    logger.info(
        f"Sync complete: {stored} records stored, {with_hrv} with HRV data"
    )

    return {
        "wellness_records": stored,
        "with_hrv": with_hrv,
    }


if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config.setup()

    days = 90
    if len(_sys.argv) > 1:
        days = int(_sys.argv[1])

    counts = sync_garmin(days=days)
    print(f"Done. Wellness: {counts['wellness_records']}, With HRV: {counts['with_hrv']}")