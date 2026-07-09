"""
Garmin Connect API client using python-garminconnect.

Pulls daily wellness data (HRV/RMSSD, RHR, stress, sleep, steps, weight)
and activity streams (power, heart rate) directly from Garmin Connect.

This is the primary source for HRV data — Garmin's data export does not
include HRV, so the API is needed for the readiness engine. Activity
streams enable W', durability, and decoupling analysis.

Usage:
    from src.ingestion.garmin_connect import sync_garmin
    sync_garmin(days=90)
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta

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
        if not sys.stdin.isatty():
            logger.warning("Non-interactive mode: cannot prompt for MFA code. "
                           "Skipping Garmin sync.")
            raise SystemExit("MFA required but running non-interactively. "
                             "Run manually or pre-cache tokens.")
        code = input("  Enter Garmin MFA code: ").strip()
        return code

    try:
        # Try to use cached tokens first
        client.login(prompt_mfa=prompt_mfa)
    except Exception as e:
        logger.error(f"Garmin login failed: {type(e).__name__}")
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
                # Sleep duration in seconds -> hours
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


def _fetch_activity_streams(
    client: "garminconnect.Garmin",
    activity_id: int,
    db_path: str,
) -> int:
    """
    Download and store activity stream data (power, heart rate) for a single activity.

    Uses get_activity_splits() to get per-split data, then downloads the FIT file
    for per-second power/HR data if available.

    Returns the number of stream records stored.
    """
    try:
        # Try to get split-level data first
        splits = client.get_activity_splits(activity_id)
        if not splits:
            return 0

        db = CyclingDB(db_path)
        total_stored = 0

        # Extract power and HR from splits
        # Each split has: startTime, duration, avgPower, avgHeartRate, etc.
        for split in splits:
            start_time = split.get("startTime", 0)
            duration = split.get("duration", 0)
            avg_power = split.get("avgPower")
            avg_hr = split.get("avgHeartRate")

            if start_time and duration:
                # Store as stream data (timestamp, value)
                power_values = []
                hr_values = []

                # Create interpolated samples within the split duration
                # Use 1-second intervals for reasonable resolution
                for sec in range(0, int(duration), 1):
                    ts = start_time + sec
                    if avg_power is not None:
                        power_values.append((float(ts), float(avg_power)))
                    if avg_hr is not None:
                        hr_values.append((float(ts), float(avg_hr)))

                if power_values:
                    total_stored += db.store_activity_streams(str(activity_id), "power", power_values)
                if hr_values:
                    total_stored += db.store_activity_streams(str(activity_id), "heart_rate", hr_values)

        db.close()
        return total_stored

    except Exception as e:
        logger.debug(f"Failed to fetch streams for activity {activity_id}: {type(e).__name__}")
        return 0


def sync_activities(
    days: int = 90,
    db_path: str | None = None,
    tokenstore: str | None = None,
) -> dict[str, int]:
    """
    Sync activity stream data (power, heart rate) from Garmin Connect.

    Downloads split-level data for recent activities and stores power/HR streams.

    Args:
        days: Number of days back to fetch activities.
        db_path: Optional override for the database path.
        tokenstore: Optional path for Garmin auth token cache.

    Returns:
        Dict with counts of activities processed and stream records stored.
    """
    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    if tokenstore is None:
        vault = config.vault_path()
        tokenstore = str(vault / "garmin_tokens.json")

    email, password, _ = _get_garmin_credentials()
    if not email or not password:
        logger.error(
            "Garmin credentials not set in config.env. "
            "Run 'python setup.py' and set GARMIN_EMAIL and GARMIN_PASSWORD."
        )
        raise SystemExit(1)

    logger.info(f"Syncing activity streams for last {days} days...")

    client = _create_client(tokenstore)
    _login(client)

    # Get activities for the date range
    today = datetime.now().date()
    start_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = today.strftime("%Y-%m-%d")

    try:
        activities = client.get_activities_by_date(start_date, end_date, None, 0, 100)
    except Exception as e:
        logger.error(f"Failed to fetch activities: {type(e).__name__}")
        return {"activities_processed": 0, "stream_records": 0}

    if not activities:
        logger.info("No activities found for the date range")
        return {"activities_processed": 0, "stream_records": 0}

    total_stored = 0
    processed = 0

    for activity in activities:
        activity_id = activity.get("activityId")
        if not activity_id:
            continue

        processed += 1
        logger.info(f"  Fetching streams for activity {activity_id} ({processed}/{len(activities)})")

        stored = _fetch_activity_streams(client, activity_id, db_path)
        total_stored += stored

        # Rate limiting
        time.sleep(1.0)

    logger.info(
        f"Activity sync complete: {processed} activities, {total_stored} stream records stored"
    )

    return {
        "activities_processed": processed,
        "stream_records": total_stored,
    }


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

        time.sleep(0.5)

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