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

try:
    from garmin_auth import GarminAuth
except ImportError:
    GarminAuth = None  # type: ignore

try:
    from fitparse import FitFile
except ImportError:
    FitFile = None  # type: ignore


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
    """Create and return an authenticated Garmin client using garmin-auth."""
    if garminconnect is None:
        raise ImportError(
            "garminconnect package not installed. "
            "Run: pip install garminconnect curl_cffi"
        )
    if GarminAuth is None:
        raise ImportError(
            "garmin-auth package not installed. "
            "Run: pip install garmin-auth"
        )

    email, password, _ = _get_garmin_credentials()

    # Use garmin-auth for token persistence and rate-limit-aware auth.
    # Tokens are cached in the vault directory so cron runs don't re-auth.
    auth = GarminAuth(
        email=email,
        password=password,
        token_dir=tokenstore if tokenstore else "~/.garminconnect",
        prompt_mfa=lambda: _prompt_mfa_interactive(),
    )

    client = auth.login()
    if client is None:
        raise RuntimeError("Garmin login returned no client")

    return client


def _prompt_mfa_interactive() -> str:
    """Prompt for MFA code; exit gracefully in non-interactive mode."""
    if not sys.stdin.isatty():
        logger.warning("Non-interactive mode: cannot prompt for MFA code. "
                       "Skipping Garmin sync.")
        raise SystemExit("MFA required but running non-interactively. "
                         "Run manually or pre-cache tokens.")
    code = input("  Enter Garmin MFA code: ").strip()
    return code

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
    Download the original FIT file for an activity, save it locally,
    and parse per-second power/HR/cadence/speed data into the DB.

    Returns the number of stream records stored.
    """
    if FitFile is None:
        logger.warning("fitparse not installed — cannot parse FIT files")
        return 0

    try:
        vault = config.vault_path()
        fit_dir = vault / "raw" / "fit"
        fit_dir.mkdir(parents=True, exist_ok=True)

        fit_path = fit_dir / f"{activity_id}.fit"

        if not fit_path.exists():
            # Download original FIT from Garmin (returns a ZIP)
            zip_bytes = client.download_activity(
                str(activity_id),
                garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL,
            )

            # Extract the .fit file from the ZIP
            import io
            import zipfile

            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                # Find the FIT file in the archive
                fit_names = [n for n in zf.namelist() if n.endswith(".fit")]
                if not fit_names:
                    logger.debug(
                        f"No .fit file in download for activity {activity_id}, "
                        f"archive contains: {zf.namelist()[:5]}"
                    )
                    return 0
                fit_name = fit_names[0]
                fit_data = zf.read(fit_name)

            fit_path.write_bytes(fit_data)
            logger.info(f"Downloaded FIT file for activity {activity_id} ({fit_name})")

        # Parse FIT file from disk
        fit_file = FitFile(str(fit_path))

        # Collect per-second data from record messages.
        # FIT timestamps are absolute UTC (systime), so we normalize to
        # elapsed seconds from the first record.
        power_values: list[tuple[float, float]] = []
        hr_values: list[tuple[float, float]] = []
        cadence_values: list[tuple[float, float]] = []
        speed_values: list[tuple[float, float]] = []
        altitude_values: list[tuple[float, float]] = []

        first_ts: float | None = None

        for msg in fit_file.get_messages("record"):
            ts = msg.get_value("timestamp")
            if ts is None:
                continue

            # fitparse returns datetime.datetime for timestamp
            if hasattr(ts, "timestamp"):
                ts = ts.timestamp()

            if first_ts is None:
                first_ts = float(ts)

            elapsed = float(ts) - first_ts

            pwr = msg.get_value("power")
            if pwr is not None:
                power_values.append((elapsed, float(pwr)))

            hr = msg.get_value("heart_rate")
            if hr is not None:
                hr_values.append((elapsed, float(hr)))

            cad = msg.get_value("cadence")
            if cad is not None:
                cadence_values.append((elapsed, float(cad)))

            speed = msg.get_value("enhanced_speed") or msg.get_value("speed")
            if speed is not None:
                speed_values.append((elapsed, float(speed)))

            alt = msg.get_value("enhanced_altitude") or msg.get_value("altitude")
            if alt is not None:
                altitude_values.append((elapsed, float(alt)))

        fit_file.close()

        if not any([power_values, hr_values, cadence_values, speed_values, altitude_values]):
            logger.debug(f"No stream data in FIT file for activity {activity_id}")
            return 0

        db = CyclingDB(db_path)
        total_stored = 0

        for metric, values in [
            ("power", power_values),
            ("heart_rate", hr_values),
            ("cadence", cadence_values),
            ("speed", speed_values),
            ("altitude", altitude_values),
        ]:
            if values:
                total_stored += db.store_activity_streams(str(activity_id), metric, values)

        db.close()
        logger.info(
            f"Parsed FIT for activity {activity_id}: "
            f"{len(power_values)} power, {len(hr_values)} HR, "
            f"{len(cadence_values)} cadence, {len(speed_values)} speed, "
            f"{len(altitude_values)} altitude samples"
        )
        return total_stored

    except Exception as e:
        logger.debug(f"Failed to fetch streams for activity {activity_id}: {type(e).__name__}: {e}")
        return 0


def sync_activities(
    days: int = 1,
    db_path: str | None = None,
    tokenstore: str | None = None,
) -> dict[str, int]:
    """
    Sync activity stream data from Garmin Connect, one day at a time,
    going backwards from the last sync point until rate-limited.

    Downloads and parses FIT files for each activity found on each day.
    Stops when Garmin returns 429 Too Many Requests, saving progress
    so the next run resumes from where it left off.

    Args:
        days: Unused (kept for backward compatibility).
        db_path: Optional override for the database path.
        tokenstore: Unused (kept for backward compatibility).

    Returns:
        Dict with counts of activities processed and stream records stored.
    """
    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    email, password, _ = _get_garmin_credentials()
    if not email or not password:
        logger.error(
            "Garmin credentials not set in config.env. "
            "Run 'python setup.py' and set GARMIN_EMAIL and GARMIN_PASSWORD."
        )
        raise SystemExit(1)

    db = CyclingDB(db_path)
    last_synced = db.get_last_synced("garmin_activities")
    db.close()

    today = datetime.now().date()
    if last_synced:
        try:
            last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"Unparseable last_synced value '{last_synced}', "
                           "starting from yesterday")
            last_date = today - timedelta(days=1)
    else:
        last_date = today - timedelta(days=1)

    client = _create_client(tokenstore)

    total_processed = 0
    total_stored = 0
    current_date = last_date - timedelta(days=1)

    while True:
        target_str = current_date.strftime("%Y-%m-%d")
        logger.info(f"Syncing activities for {target_str}")

        try:
            activities = client.get_activities_by_date(target_str, target_str)
        except garminconnect.GarminConnectTooManyRequestsError as e:
            logger.warning(f"Rate limited by Garmin Connect: {e}")
            # Save progress so next run resumes from this day
            # (we haven't processed this day yet, so don't advance)
            logger.info(f"Pausing — {total_processed} activities processed, "
                        f"{total_stored} stream records stored so far")
            break
        except Exception as e:
            logger.error(f"Failed to fetch activities for {target_str}: "
                         f"{type(e).__name__}: {e}")
            # On non-rate-limit errors, advance to avoid infinite loop
            db = CyclingDB(db_path)
            db.set_last_synced("garmin_activities", target_str)
            db.close()
            current_date -= timedelta(days=1)
            time.sleep(2.0)
            continue

        if not activities:
            logger.info(f"No activities found for {target_str}")
            # Advance the sync pointer so we don't retry forever
            db = CyclingDB(db_path)
            db.set_last_synced("garmin_activities", target_str)
            db.close()
            current_date -= timedelta(days=1)
            time.sleep(0.5)
            continue

        day_processed = 0
        day_stored = 0

        for activity in activities:
            activity_id = activity.get("activityId")
            if not activity_id:
                continue

            day_processed += 1
            logger.info(
                f"  Fetching streams for activity {activity_id} "
                f"({day_processed}/{len(activities)})"
            )

            try:
                stored = _fetch_activity_streams(client, activity_id, db_path)
                day_stored += stored
            except garminconnect.GarminConnectTooManyRequestsError as e:
                logger.warning(f"Rate limited during activity {activity_id}: {e}")
                # Save progress at the activity level
                total_processed += day_processed
                total_stored += day_stored
                logger.info(f"Pausing mid-day — {total_processed} activities processed, "
                            f"{total_stored} stream records stored so far")
                return {
                    "activities_processed": total_processed,
                    "stream_records": total_stored,
                }
            except Exception as e:
                logger.warning(f"Failed to fetch streams for {activity_id}: "
                               f"{type(e).__name__}: {e}")

            time.sleep(1.0)

        total_processed += day_processed
        total_stored += day_stored

        # Record sync timestamp for this day
        db = CyclingDB(db_path)
        db.set_last_synced("garmin_activities", target_str)
        db.close()

        logger.info(
            f"Day {target_str} complete: {day_processed} activities, "
            f"{day_stored} stream records (total: {total_processed} activities, "
            f"{total_stored} records)"
        )

        # Move to the previous day
        current_date -= timedelta(days=1)
        time.sleep(0.5)

    return {
        "activities_processed": total_processed,
        "stream_records": total_stored,
    }


def sync_garmin(
    days: int = 1,
    db_path: str | None = None,
    tokenstore: str | None = None,
) -> dict[str, int]:
    """
    Sync wellness data from Garmin Connect, one day at a time.

    Pulls the single most recent day that hasn't been synced yet
    (going backwards from the last sync point). On first run with no
    prior sync, pulls yesterday. This lets cron run daily and
    incrementally backfill historical data without overwhelming the API.

    Args:
        days: Unused (kept for backward compatibility).
        db_path: Optional override for the database path.
        tokenstore: Unused (kept for backward compatibility).

    Returns:
        Dict with counts of new/updated records.
    """
    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    email, password, _ = _get_garmin_credentials()
    if not email or not password:
        logger.error(
            "Garmin credentials not set in config.env. "
            "Run 'python setup.py' and set GARMIN_EMAIL and GARMIN_PASSWORD."
        )
        raise SystemExit(1)

    db = CyclingDB(db_path)
    last_synced = db.get_last_synced("garmin_wellness")

    today = datetime.now().date()
    if last_synced:
        try:
            last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"Unparseable last_synced value '{last_synced}', "
                           "starting from yesterday")
            last_date = today - timedelta(days=1)
    else:
        last_date = today - timedelta(days=1)

    # Target the day before the last synced day
    target_date = last_date - timedelta(days=1)
    target_str = target_date.strftime("%Y-%m-%d")

    logger.info(f"Syncing wellness for {target_str} (last synced: {last_synced or 'never'})")

    # Create client and login
    client = _create_client(tokenstore)
    logger.info("Authenticated with Garmin Connect")

    record = fetch_wellness_for_date(client, target_str)
    time.sleep(0.5)

    if record is None:
        logger.info(f"No wellness data for {target_str}")
        # Still advance the sync pointer so we don't retry forever
        db.set_last_synced("garmin_wellness", target_str)
        db.close()
        return {"wellness_records": 0, "with_hrv": 0}

    stored = db.store_wellness([record])
    db.set_last_synced("garmin_wellness", target_str)
    db.close()

    with_hrv = 1 if record.get("rmssd") is not None else 0

    logger.info(
        f"Sync complete for {target_str}: {stored} record(s) stored, "
        f"{with_hrv} with HRV data"
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