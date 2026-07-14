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
from datetime import date, datetime, timedelta
from typing import Any, Callable

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
    import fitdecode
except ImportError:
    fitdecode = None  # type: ignore


def _safe_timestamp_to_date(ts_ms: float | int | None) -> str | None:
    """Convert a millisecond timestamp to 'YYYY-MM-DD', safely.

    Returns None if the timestamp is missing, zero, negative, or out of
    the representable range for datetime.fromtimestamp().
    """
    if ts_ms is None:
        return None
    try:
        ts_sec = float(ts_ms) / 1000
    except (TypeError, ValueError):
        return None
    if ts_sec <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts_sec).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


class RateLimiter:
    """Proactive rate limiting for Garmin Connect API calls.

    Enforces a minimum interval between calls and adapts on 429 responses
    with exponential backoff. Thread-safe via time.monotonic.

    Default parameters are conservative for Garmin's undocumented limits:
    - 1 second between calls (60 req/min ceiling)
    - On 429: back off 2x the Retry-After header or 2x the current interval
    - Cap backoff at 300s to avoid indefinite stalls
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        max_backoff: float = 300.0,
        backoff_factor: float = 2.0,
    ):
        self.min_interval = min_interval
        self.max_backoff = max_backoff
        self.backoff_factor = backoff_factor
        self._effective_interval = min_interval
        self._last_call: float = 0.0

    def wait(self) -> None:
        """Block until the minimum interval has elapsed since the last call."""
        now = time.monotonic()
        if self._last_call > 0:
            elapsed = now - self._last_call
            sleep_time = self._effective_interval - elapsed
            if sleep_time > 0:
                logger.debug(
                    f"RateLimiter: sleeping {sleep_time:.2f}s "
                    f"(interval={self._effective_interval:.1f}s)"
                )
                time.sleep(sleep_time)
        self._last_call = time.monotonic()

    def record_429(self, retry_after: float | None = None) -> None:
        """Called when a 429 is received; increases the effective interval.

        Args:
            retry_after: Optional Retry-After header value in seconds.
        """
        if retry_after is not None and retry_after > 0:
            self._effective_interval = min(retry_after, self.max_backoff)
        else:
            self._effective_interval = min(
                self._effective_interval * self.backoff_factor,
                self.max_backoff,
            )
        logger.warning(
            f"RateLimiter: 429 received, new interval={self._effective_interval:.1f}s"
        )

    def reset(self) -> None:
        """Reset to the base interval. Call between sync sessions."""
        self._effective_interval = self.min_interval
        self._last_call = 0.0


# Module-level rate limiter — shared across all Garmin API calls in a session.
_rate_limiter = RateLimiter()


def rate_limiter() -> RateLimiter:
    """Access the module-level rate limiter."""
    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the rate limiter to base settings. Call at the start of a sync session."""
    _rate_limiter.reset()


def _retry_on_rate_limit(fn, max_retries: int = 3):
    """Call *fn* with retry on 429, using the rate limiter's backoff interval.

    On each 429 the rate limiter's interval is increased (exponential backoff),
    then we sleep for that interval before retrying. After *max_retries* failures
    the original exception is re-raised.
    """
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            if attempt == max_retries:
                logger.warning(
                    f"Rate limit retry exhausted after {max_retries} attempts: {e}"
                )
                raise
            wait = min(_rate_limiter._effective_interval, _rate_limiter.max_backoff)
            logger.info(
                f"Rate limited (attempt {attempt}/{max_retries}), "
                f"retrying in {wait:.1f}s..."
            )
            time.sleep(wait)


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


def _create_client(tokenstore: str | None = None) -> "garminconnect.Garmin":
    """Create and return an authenticated Garmin client using garmin-auth.

    Reuses cached tokens when available to avoid re-authenticating (and
    triggering MFA) on every sync. Falls back to full login only when
    no cached tokens exist.
    """
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

    # Use GARMIN_TOKENSTORE env var if not explicitly provided
    if tokenstore is None:
        tokenstore = os.getenv("GARMIN_TOKENSTORE", "")

    # Use garmin-auth for token persistence and rate-limit-aware auth.
    # Tokens are cached in the vault directory so cron runs don't re-auth.
    # Use return_on_mfa=True to avoid interactive prompts in non-interactive contexts.
    auth = GarminAuth(
        email=email,
        password=password,
        token_dir=tokenstore if tokenstore else "~/.garminconnect",
        return_on_mfa=True,
    )

    # auth.login() tries cached tokens first (via _try_cached_login internally),
    # then falls back to fresh login if needed. If MFA is required, it returns
    # "needs_mfa" instead of prompting interactively.
    result = auth.login()

    if result == "needs_mfa":
        raise RuntimeError(
            "Garmin login requires MFA but running non-interactively. "
            "Please log in via the Settings page first to cache tokens."
        )

    return result


def _prompt_mfa_interactive() -> str:
    """Prompt for MFA code; raise in non-interactive mode."""
    if not sys.stdin.isatty():
        raise RuntimeError(
            "MFA required but running non-interactively. "
            "Run manually or pre-cache tokens."
        )
    code = input("  Enter Garmin MFA code: ").strip()
    return code


class GarminAuthResult:
    """Result of a Garmin authentication attempt from the UI."""

    def __init__(self, success: bool, mfa_required: bool = False, error: str = ""):
        self.success = success
        self.mfa_required = mfa_required
        self.error = error


def authenticate_garmin(
    email: str,
    password: str,
    tokenstore: str,
    mfa_code: str | None = None,
    auth_instance: object | None = None,
) -> tuple["GarminAuthResult", object | None]:
    """Authenticate with Garmin Connect using garmin-auth 0.3.0 API.

    Phase 1 (initial login): pass email/password only.
        Returns (result, auth_instance). If MFA required, save auth_instance.

    Phase 2 (MFA completion): pass the saved auth_instance + mfa_code.
        Calls auth.resume_login(code) on the same instance.

    On success, tokens are persisted in *tokenstore* for future use.
    """
    if GarminAuth is None:
        return GarminAuthResult(
            success=False,
            error="garmin-auth package not installed.",
        ), None

    # ── Phase 2: resume pending MFA login ──────────────────────────────
    if mfa_code is not None and auth_instance is not None:
        try:
            auth_instance.resume_login(mfa_code)
            return GarminAuthResult(success=True), auth_instance
        except ValueError as e:
            return GarminAuthResult(success=False, error=str(e)), auth_instance
        except Exception as e:
            err_msg = str(e)
            if any(kw in err_msg.lower() for kw in ["mfa", "two-factor", "verification", "invalid"]):
                return GarminAuthResult(success=False, error="Invalid verification code."), auth_instance
            if "429" in err_msg or "rate limited" in err_msg.lower():
                return GarminAuthResult(
                    success=False,
                    error="Garmin has temporarily blocked login from this IP. "
                           "Wait 30-60 minutes and try again.",
                ), auth_instance
            return GarminAuthResult(success=False, error=err_msg), auth_instance

    # ── Phase 1: initial login ─────────────────────────────────────────
    try:
        auth = GarminAuth(
            email=email,
            password=password,
            token_dir=tokenstore if tokenstore else "~/.garminconnect",
            return_on_mfa=True,
        )
        result = auth.login()

        if result == "needs_mfa":
            return GarminAuthResult(success=False, mfa_required=True), auth

        # result is a Garmin client → success
        return GarminAuthResult(success=True), auth

    except Exception as e:
        err_msg = str(e)
        if any(kw in err_msg.lower() for kw in ["mfa", "two-factor", "verification code", "second factor"]):
            return GarminAuthResult(success=False, mfa_required=True), None
        if "429" in err_msg or "rate limited" in err_msg.lower():
            return GarminAuthResult(
                success=False,
                error="Garmin has temporarily blocked login from this IP due to too many attempts. "
                       "Wait 30-60 minutes and try again.",
            ), None
        return GarminAuthResult(success=False, error=err_msg), None

def fetch_wellness_for_date(
    client: "garminconnect.Garmin", date_str: str
) -> dict[str, Any] | None:
    """
    Fetch all wellness data for a single date from Garmin Connect.

    Returns a dict compatible with the wellness table schema, or None
    if no data is available for that date.
    """
    rl = _rate_limiter
    try:
        # Get basic stats (RHR, steps, stress)
        rl.wait()
        stats = _retry_on_rate_limit(lambda: client.get_stats(date_str))

        # Get HRV data
        rl.wait()
        hrv_data = _retry_on_rate_limit(lambda: client.get_hrv_data(date_str))

        # Get heart rates (for RHR verification)
        rl.wait()
        heart_rates = _retry_on_rate_limit(lambda: client.get_heart_rates(date_str))

        # Get body composition (for weight)
        try:
            rl.wait()
            body = _retry_on_rate_limit(lambda: client.get_body_composition(date_str))
        except Exception:
            body = None

        # Get weigh-ins (more reliable for weight)
        try:
            rl.wait()
            weigh_ins = _retry_on_rate_limit(
                lambda: client.get_daily_weigh_ins(date_str)
            )
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
            # Garmin returns lastNight as the primary RMSSD value
            rmssd = summary.get("lastNightAvg")

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
            rl.wait()
            sleep_data = _retry_on_rate_limit(lambda: client.get_sleep_data(date_str))
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
    db: CyclingDB,
) -> int:
    """
    Download the original FIT file for an activity, save it locally,
    and parse per-second power/HR/cadence/speed data into the DB.

    Returns the number of stream records stored.
    """
    if fitdecode is None:
        logger.warning("fitdecode not installed — cannot parse FIT files")
        return 0

    try:
        vault = config.vault_path()
        fit_dir = vault / "raw" / "fit"
        fit_dir.mkdir(parents=True, exist_ok=True)

        fit_path = fit_dir / f"{activity_id}.fit"

        if not fit_path.exists():
            # Download original FIT from Garmin (returns a ZIP)
            _rate_limiter.wait()
            zip_bytes = _retry_on_rate_limit(
                lambda: client.download_activity(
                    str(activity_id),
                    garminconnect.Garmin.ActivityDownloadFormat.ORIGINAL,
                )
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

        # Parse FIT file from disk using fitdecode
        power_values: list[tuple[float, float]] = []
        hr_values: list[tuple[float, float]] = []
        cadence_values: list[tuple[float, float]] = []
        speed_values: list[tuple[float, float]] = []
        altitude_values: list[tuple[float, float]] = []

        first_ts: float | None = None

        def _get_field(frame, name):
            try:
                return frame.get_field(name)
            except KeyError:
                return None

        with fitdecode.FitReader(str(fit_path)) as fit:
            for frame in fit:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name != "record":
                    continue

                ts_field = _get_field(frame, "timestamp")
                if ts_field is None or ts_field.value is None:
                    continue
                ts = ts_field.value

                # fitdecode returns datetime.datetime for timestamp
                if hasattr(ts, "timestamp"):
                    ts = ts.timestamp()

                if first_ts is None:
                    first_ts = float(ts)

                elapsed = float(ts) - first_ts

                pwr_field = _get_field(frame, "power")
                if pwr_field is not None and pwr_field.value is not None:
                    power_values.append((elapsed, float(pwr_field.value)))

                hr_field = _get_field(frame, "heart_rate")
                if hr_field is not None and hr_field.value is not None:
                    hr_values.append((elapsed, float(hr_field.value)))

                cad_field = _get_field(frame, "cadence")
                if cad_field is not None and cad_field.value is not None:
                    cadence_values.append((elapsed, float(cad_field.value)))

                speed_field = _get_field(frame, "enhanced_speed")
                if speed_field is None:
                    speed_field = _get_field(frame, "speed")
                if speed_field is not None and speed_field.value is not None:
                    speed_values.append((elapsed, float(speed_field.value)))

                alt_field = _get_field(frame, "enhanced_altitude")
                if alt_field is None:
                    alt_field = _get_field(frame, "altitude")
                if alt_field is not None and alt_field.value is not None:
                    altitude_values.append((elapsed, float(alt_field.value)))

        if not any([power_values, hr_values, cadence_values, speed_values, altitude_values]):
            logger.debug(f"No stream data in FIT file for activity {activity_id}")
            return 0

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



def _garmin_activity_to_store_format(activity: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a Garmin API activity dict to the format expected by db.store_activities()."""
    activity_id = activity.get("activityId")
    if not activity_id:
        return None

    start_time_local = activity.get("startTimeLocal", "")

    activity_type_raw = activity.get("activityType", {})
    if isinstance(activity_type_raw, dict):
        activity_type_key = activity_type_raw.get("typeKey", "")
    else:
        activity_type_key = str(activity_type_raw)

    type_map = {
        "cycling": "Ride",
        "running": "Run",
        "walking": "Walk",
        "swimming": "Swim",
        "indoor_cycling": "Indoor Cycle",
        "virtual_ride": "Virtual Ride",
        "strength_training": "Strength Training",
    }
    activity_type = type_map.get(
        activity_type_key.lower(),
        activity_type_key.title() if activity_type_key else "Activity",
    )

    return {
        "id": f"garmin_{activity_id}",
        "start_date_local": start_time_local,
        "type": activity_type,
        "duration": activity.get("duration"),
        "distance": activity.get("distance"),
        "average_power": activity.get("avgPower"),
        "max_power": activity.get("maxPower"),
        "average_hr": activity.get("avgHeartRate"),
        "max_hr": activity.get("maxHeartRate"),
        "calories": activity.get("calories"),
        "tss": activity.get("trainingStressScore"),
        "ifr": None,
        "normalized_power": activity.get("normPower"),
        "file_type": None,
    }


def _sync_activities_batch(
    client: "garminconnect.Garmin",
    db: CyclingDB,
    last_synced_date: "date | None",
    unbounded: bool,
    progress_callback: "Callable[[int, str], None] | None" = None,
) -> tuple[int, int]:
    """Fetch activities in batches and process FIT streams.

    Phase 1 (Blueprint): Fetch activity summaries in batches of 100 via
    get_activities(start, limit). Build a list of new activity IDs.
    Phase 1.5 (Store Summaries): Write activity summaries to the activities
    table so they appear in the Activity Detail page.
    Phase 2 (Download): For each new activity, download FIT file and
    parse streams via _fetch_activity_streams().

    Returns (total_processed, total_stored).
    """
    from datetime import date

    # Check for a saved resume offset from a previous interrupted run
    resume_offset = db.get_resume_offset("garmin_activities")

    # --- Phase 1: Activity Discovery ---
    new_activities: list[dict[str, Any]] = []
    batch_size = 100
    offset = 0
    date_cutoff_reached = False

    # Try to get total count for progress estimation
    try:
        total_count = client.count_activities()
    except Exception:
        total_count = None

    total_batches = None
    if total_count is not None:
        total_batches = (total_count + batch_size - 1) // batch_size

    logger.info(
        f"Phase 1: Fetching activity list"
        f"{f' (total ~{total_count})' if total_count is not None else ''}"
    )

    while True:
        # Check if we have a resume offset to start from
        if offset == 0 and resume_offset > 0:
            offset = resume_offset
            logger.info(f"Resuming from saved offset {resume_offset}")
            resume_offset = 0  # consume the resume offset

        try:
            _rate_limiter.wait()
            activities = _retry_on_rate_limit(
                lambda: client.get_activities(start=offset, limit=batch_size)
            )
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during activity discovery: {e}")
            if offset > 0:
                db.set_last_synced(
                    "garmin_activities",
                    db.get_last_synced("garmin_activities") or datetime.now().date().isoformat(),
                    resume_offset=offset,
                )
            db.close()
            return (len(new_activities), 0)
        except Exception as e:
            logger.error(f"Failed to fetch activity batch at offset {offset}: {type(e).__name__}: {e}")
            break

        if not activities:
            break

        batch_activities = list(activities) if not isinstance(activities, list) else activities

        if not batch_activities:
            break

        batch_index = offset // batch_size
        if progress_callback is not None and total_batches is not None:
            pct = int(batch_index / max(total_batches, 1) * 50)
            progress_callback(pct, f"Fetching activities... ({len(new_activities)} found so far)")

        for activity in batch_activities:
            # Extract activity date from startTimeLocal
            start_time_local = activity.get("startTimeLocal", "")
            activity_date: "date | None" = None
            if start_time_local:
                try:
                    activity_date = datetime.strptime(start_time_local[:10], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    pass

            # Incremental mode: skip activities at or before last_synced_date
            if not unbounded and last_synced_date is not None and activity_date is not None:
                if activity_date <= last_synced_date:
                    logger.info(
                        f"Activity {activity_date} <= last synced {last_synced_date}, "
                        "stopping discovery"
                    )
                    date_cutoff_reached = True
                    break

            new_activities.append(activity)

        if date_cutoff_reached:
            break

        if not batch_activities or len(batch_activities) < batch_size:
            # Last batch or fewer results than requested
            break

        offset += batch_size

        # Safety: if we've gone way past the total count, stop
        if total_count is not None and offset > total_count:
            break

    logger.info(f"Phase 1 complete: {len(new_activities)} new activities to process")

    # --- Phase 1.5: Store Activity Summaries ---
    if new_activities:
        store_records = []
        for activity in new_activities:
            rec = _garmin_activity_to_store_format(activity)
            if rec is not None:
                store_records.append(rec)
        if store_records:
            stored_count = db.store_activities(store_records)
            logger.info(f"Phase 1.5: Stored {stored_count} activity summaries")

    if progress_callback is not None:
        progress_callback(50, "Downloaded activity list, now processing FIT files...")

    # --- Phase 2: FIT Download and Processing ---
    total_processed = 0
    total_stored = 0

    for i, activity in enumerate(new_activities):
        activity_id = activity.get("activityId")
        if not activity_id:
            continue

        start_time_local = activity.get("startTimeLocal", "")
        activity_date_str = start_time_local[:10] if start_time_local else "unknown"

        logger.info(
            f"Phase 2: Processing activity {activity_id} ({activity_date_str}) "
            f"({i + 1}/{len(new_activities)})"
        )

        try:
            _rate_limiter.wait()
            time.sleep(1.0)  # spread load between downloads
            stored = _fetch_activity_streams(client, activity_id, db)
            total_processed += 1
            total_stored += stored
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during activity {activity_id}: {e}")
            db.set_last_synced(
                "garmin_activities",
                activity_date_str,
                resume_offset=i,
            )
            db.close()
            return (total_processed, total_stored)
        except Exception as e:
            logger.warning(f"Failed to fetch streams for {activity_id}: {type(e).__name__}: {e}")
            total_processed += 1

        if progress_callback is not None:
            pct = 50 + int(i / max(len(new_activities), 1) * 45)
            progress_callback(
                min(pct, 95),
                f"Processing FIT: {activity_date_str} ({i + 1}/{len(new_activities)})",
            )

    # Clear any resume offset on successful completion
    if new_activities:
        last_activity_date = ""
        for activity in reversed(new_activities):
            stl = activity.get("startTimeLocal", "")
            if stl:
                last_activity_date = stl[:10]
                break
        db.set_last_synced(
            "garmin_activities",
            last_activity_date or datetime.now().date().isoformat(),
            resume_offset=0,
        )

    logger.info(
        f"Phase 2 complete: {total_processed} activities processed, "
        f"{total_stored} stream records stored"
    )

    return (total_processed, total_stored)

def sync_activities(
    days: int = 1,
    db_path: str | None = None,
    tokenstore: str | None = None,
    unbounded: bool = False,
    progress_callback: "Callable[[int, str], None] | None" = None,
) -> dict[str, int]:
    """
    Sync activity stream data from Garmin Connect using batch pagination.

    Uses get_activities(start, limit) to fetch activity summaries in batches,
    then downloads and parses FIT files for each new activity. This replaces
    the previous day-by-day approach, reducing API calls from ~100 to ~1-2
    for typical incremental syncs.

    When *unbounded* is True, syncs all activities until rate-limited
    (no day limit).

    Stops when Garmin returns 429 Too Many Requests, saving progress
    (including resume offset) so the next run resumes from where it left off.

    Args:
        days: Unused (kept for backward compatibility).
        db_path: Optional override for the database path.
        tokenstore: Optional override for the Garmin token directory.
        unbounded: If True, sync indefinitely until rate-limited.
        progress_callback: Optional callback(progress_pct, message) for UI updates.

    Returns:
        Dict with counts of activities processed and stream records stored.
    """
    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    email, password, _ = _get_garmin_credentials()
    if not email or not password:
        raise RuntimeError(
            "Garmin credentials not set in config.env. "
            "Run 'python setup.py' and set GARMIN_EMAIL and GARMIN_PASSWORD."
        )

    db = CyclingDB(db_path)
    last_synced = db.get_last_synced("garmin_activities")

    today = datetime.now().date()

    # Parse last sync date for incremental mode
    if last_synced:
        try:
            last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
        except ValueError:
            logger.warning(f"Unparseable last_synced value '{last_synced}', "
                           "starting from yesterday")
            last_date = today - timedelta(days=1)
    else:
        last_date = today - timedelta(days=1)

    reset_rate_limiter()
    client = _create_client(tokenstore)

    # For incremental mode, pass last_date as the cutoff.
    # For unbounded mode, pass None to process everything.
    last_synced_date = last_date if not unbounded else None

    total_processed, total_stored = _sync_activities_batch(
        client, db, last_synced_date, unbounded, progress_callback,
    )

    # If the DB was already closed by _sync_activities_batch (rate limit),
    # don't try to update state again
    try:
        if total_processed > 0:
            # Update last_synced to today on successful processing
            db.set_last_synced("garmin_activities", today.isoformat(), resume_offset=0)
    except Exception:
        pass  # DB may already be closed

    try:
        db.close()
    except Exception:
        pass

    if progress_callback is not None:
        progress_callback(100, f"Sync complete: {total_processed} activities, {total_stored} records")

    return {
        "activities_processed": total_processed,
        "stream_records": total_stored,
    }


def sync_garmin(
    days: int = 1,
    db_path: str | None = None,
    tokenstore: str | None = None,
    unbounded: bool = False,
    progress_callback: "Callable[[int, str], None] | None" = None,
) -> dict[str, int]:
    """
    Sync wellness data from Garmin Connect using bulk fetching where possible.

    Bulk fetches weight, body composition, and steps for the entire date range
    in a single API call each. Then fetches HRV and sleep data per-day (these
    are per-day only in the Garmin API).

    When *unbounded* is True, syncs continuously until rate-limited
    (no day limit).

    Pulls days going backwards from the last sync point. On first run with no
    prior sync, starts from yesterday. This lets cron run daily and
    incrementally backfill historical data without overwhelming the API.

    Args:
        days: Number of days to sync (default 1). Ignored if *unbounded* is True.
        db_path: Optional override for the database path.
        tokenstore: Unused (kept for backward compatibility).
        unbounded: If True, sync indefinitely until rate-limited.

    Returns:
        Dict with counts of new/updated records.
    """
    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    email, password, _ = _get_garmin_credentials()
    if not email or not password:
        raise RuntimeError(
            "Garmin credentials not set in config.env. "
            "Run 'python setup.py' and set GARMIN_EMAIL and GARMIN_PASSWORD."
        )

    db = CyclingDB(db_path)
    last_synced = db.get_last_synced("garmin_wellness")

    today = datetime.now().date()

    if unbounded:
        if last_synced:
            try:
                last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
            except ValueError:
                logger.warning(f"Unparseable last_synced value '{last_synced}', "
                               "starting from yesterday")
                last_date = today - timedelta(days=1)
        else:
            last_date = today - timedelta(days=1)
    else:
        # Incremental: sync from day after last sync up to today
        if last_synced:
            try:
                last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
            except ValueError:
                logger.warning(f"Unparseable last_synced value '{last_synced}', "
                               "starting from yesterday")
                last_date = today - timedelta(days=1)
        else:
            last_date = today - timedelta(days=1)
        # Use the requested days limit, capped by the gap since last sync
        gap = max(0, (today - last_date).days)
        days = min(days, gap)
        last_date = today

    reset_rate_limiter()
    client = _create_client(tokenstore)
    logger.info("Authenticated with Garmin Connect")

    # Build list of dates to sync
    sync_dates: list[date] = []
    current_date = last_date - timedelta(days=1)
    days_synced = 0
    while unbounded or days_synced < days:
        sync_dates.append(current_date)
        current_date -= timedelta(days=1)
        days_synced += 1

    if not sync_dates:
        db.close()
        return {"wellness_records": 0, "with_hrv": 0}

    start_date = sync_dates[-1]
    end_date = sync_dates[0]
    logger.info(
        f"Syncing wellness for {len(sync_dates)} days: "
        f"{start_date} to {end_date}"
    )

    # --- Bulk fetch weight, body composition, steps ---
    weight_by_date: dict[str, float] = {}
    steps_by_date: dict[str, int] = {}

    # Bulk weigh-ins
    try:
        _rate_limiter.wait()
        weigh_ins = _retry_on_rate_limit(
            lambda: client.get_weigh_ins(
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
            )
        )
        for entry in weigh_ins:
            if not isinstance(entry, dict):
                continue
            d = entry.get("dateTimestamp")
            date_str = _safe_timestamp_to_date(d)
            w = entry.get("weightGrams")
            if w and date_str and date_str not in weight_by_date:
                weight_by_date[date_str] = w / 1000.0
    except Exception as e:
        logger.warning(f"Failed to bulk fetch weigh-ins: {e}")

    # Bulk body composition
    try:
        _rate_limiter.wait()
        body = _retry_on_rate_limit(
            lambda: client.get_body_composition(
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
            )
        )
        if isinstance(body, list):
            for entry in body:
                d = entry.get("dateTimestamp")
                date_str = _safe_timestamp_to_date(d)
                w = entry.get("weight")
                if w and date_str and date_str not in weight_by_date:
                    weight_by_date[date_str] = float(w)
    except Exception as e:
        logger.warning(f"Failed to bulk fetch body composition: {e}")

    # Bulk steps (28-day chunks)
    try:
        _rate_limiter.wait()
        steps_data = _retry_on_rate_limit(
            lambda: client.get_daily_steps(
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
            )
        )
        for entry in steps_data:
            d = entry.get("calendarDate")
            if d:
                steps_by_date[d] = entry.get("totalSteps")
    except Exception as e:
        logger.warning(f"Failed to bulk fetch steps: {e}")

    logger.info(
        f"Bulk fetch complete: {len(weight_by_date)} weight entries, "
        f"{len(steps_by_date)} step entries"
    )

    if progress_callback is not None:
        progress_callback(10, "Bulk data fetched, now fetching HRV and sleep...")

    # --- Per-day fetch HRV and sleep ---
    total_stored = 0
    total_with_hrv = 0

    for i, d in enumerate(sync_dates):
        target_str = d.strftime("%Y-%m-%d")

        # Fetch HRV
        rmssd = None
        try:
            _rate_limiter.wait()
            hrv_data = _retry_on_rate_limit(lambda: client.get_hrv_data(target_str))
            if hrv_data and "hrvSummary" in hrv_data:
                rmssd = hrv_data["hrvSummary"].get("lastNightAvg")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during HRV sync: {e}")
            db.set_last_synced("garmin_wellness", target_str)
            db.close()
            return {"wellness_records": total_stored, "with_hrv": total_with_hrv}
        except Exception as e:
            logger.debug(f"Failed to fetch HRV for {target_str}: {e}")

        # Fetch sleep
        sleep_score = None
        sleep_hours = None
        try:
            _rate_limiter.wait()
            sleep_data = _retry_on_rate_limit(lambda: client.get_sleep_data(target_str))
            if sleep_data:
                sleep_score = sleep_data.get("sleepScore")
                sleep_ms = sleep_data.get("sleepTimeSeconds", 0)
                if sleep_ms:
                    sleep_hours = sleep_ms / 3600.0
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during sleep sync: {e}")
            db.set_last_synced("garmin_wellness", target_str)
            db.close()
            return {"wellness_records": total_stored, "with_hrv": total_with_hrv}
        except Exception as e:
            logger.debug(f"Failed to fetch sleep for {target_str}: {e}")

        # Fetch stats for RHR and stress
        resting_hr = None
        stress = None
        try:
            _rate_limiter.wait()
            stats = _retry_on_rate_limit(lambda: client.get_stats(target_str))
            if stats:
                resting_hr = stats.get("restingHeartRate")
                if stats.get("allDayStress"):
                    stress = stats["allDayStress"].get("averageStressLevel")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during stats sync: {e}")
            db.set_last_synced("garmin_wellness", target_str)
            db.close()
            return {"wellness_records": total_stored, "with_hrv": total_with_hrv}
        except Exception as e:
            logger.debug(f"Failed to fetch stats for {target_str}: {e}")

        # Build wellness record from bulk + per-day data
        weight = weight_by_date.get(target_str)
        steps = steps_by_date.get(target_str)

        if not any([resting_hr, rmssd, stress, steps, weight]):
            logger.info(f"No wellness data for {target_str}")
            db.set_last_synced("garmin_wellness", target_str)
            if progress_callback is not None:
                pct = 10 + int(i / max(len(sync_dates), 1) * 80)
                progress_callback(min(pct, 95), f"Wellness: {target_str} ({i+1}/{len(sync_dates)} days)")
            time.sleep(0.5)
            continue

        record = {
            "date": target_str,
            "weight": weight,
            "resting_hr": resting_hr,
            "rmssd": rmssd,
            "stress": stress,
            "sleep_score": sleep_score,
            "sleep_hours": sleep_hours,
            "steps": steps,
        }

        stored = db.store_wellness([record])
        db.set_last_synced("garmin_wellness", target_str)

        with_hrv = 1 if rmssd is not None else 0
        total_stored += stored
        total_with_hrv += with_hrv

        logger.info(
            f"Sync complete for {target_str}: {stored} record(s) stored, "
            f"{with_hrv} with HRV data"
        )

        if progress_callback is not None:
            pct = 10 + int(i / max(len(sync_dates), 1) * 80)
            progress_callback(min(pct, 95), f"Wellness: {target_str} ({i+1}/{len(sync_dates)} days)")

        time.sleep(0.5)

    db.close()
    return {
        "wellness_records": total_stored,
        "with_hrv": total_with_hrv,
    }

if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config.setup()

    days = 90
    if len(_sys.argv) > 1:
        days = int(_sys.argv[1])

    email, password, tokenstore = _get_garmin_credentials()
    if not email or not password:
        logger.error("Garmin credentials not set. Run setup.py first.")
        raise SystemExit(1)

    from garmin_auth import GarminAuth
    auth = GarminAuth(email=email, password=password, token_dir=tokenstore, return_on_mfa=True)
    result = auth.login()

    if result == "needs_mfa":
        code = input("Enter Garmin OTP: ").strip()
        client = auth.resume_login(code)
    elif result is not None:
        client = result
    else:
        logger.error("Login failed")
        raise SystemExit(1)

    db_path = str(config.db_path("cycling_agent.sqlite"))
    db = CyclingDB(db_path)
    last_synced = db.get_last_synced("garmin_wellness")
    today = datetime.now().date()
    if last_synced:
        try:
            last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
        except ValueError:
            last_date = today - timedelta(days=1)
    else:
        last_date = today - timedelta(days=1)

    total_stored = 0
    total_with_hrv = 0
    current_date = last_date - timedelta(days=1)
    days_synced = 0

    while days_synced < days:
        target_str = current_date.strftime("%Y-%m-%d")
        logger.info(f"Syncing wellness for {target_str}")
        record = fetch_wellness_for_date(client, target_str)
        if record is None:
            logger.info(f"No wellness data for {target_str}")
            db.set_last_synced("garmin_wellness", target_str)
        else:
            stored = db.store_wellness([record])
            db.set_last_synced("garmin_wellness", target_str)
            with_hrv = 1 if record.get("rmssd") is not None else 0
            total_stored += stored
            total_with_hrv += with_hrv
        current_date -= timedelta(days=1)
        days_synced += 1
        time.sleep(0.5)

    db.close()
    print(f"Done. Wellness: {total_stored}, With HRV: {total_with_hrv}")