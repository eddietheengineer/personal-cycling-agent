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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from dataclasses import dataclass

from src import config
from src.config.constants import (
    GARMIN_RATE_LIMIT_MIN_INTERVAL,
    GARMIN_RATE_LIMIT_MAX_BACKOFF,
    GARMIN_RATE_LIMIT_BACKOFF_FACTOR,
    GARMIN_ACTIVITY_BATCH_SIZE,
    GARMIN_FIT_DOWNLOAD_INTERVAL,
    GARMIN_WELLNESS_POLL_INTERVAL,
    MAX_SYNC_DAYS,
    DEFAULT_CLI_SYNC_DAYS,
)
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


@dataclass
class FitParseResult:
    """Result of parsing a FIT file's frames."""
    power_values: list[tuple[float, float]]
    hr_values: list[tuple[float, float]]
    cadence_values: list[tuple[float, float]]
    speed_values: list[tuple[float, float]]
    altitude_values: list[tuple[float, float]]
    # Session-level metrics (raw FIT units: duration in 1/1000s)
    duration_ms: float | None
    distance_m: float | None
    sport: str | None
    avg_hr: float | None
    max_hr: float | None
    calories: float | None
    avg_cadence: float | None
    max_cadence: float | None
    avg_power: float | None
    max_power: float | None
    # Optional: power meter device info (only set by _fetch_activity_streams)
    power_meter: str | None = None


def _parse_fit_frames(fit_path: "Path", extract_power_meter: bool = False) -> FitParseResult | None:
    """Parse a FIT file and return session metrics + per-second stream data.

    Shared by _fetch_activity_streams and _parse_fit_file.
    Returns None if fitdecode is not available or the file cannot be read.
    """
    if fitdecode is None:
        return None

    power_values: list[tuple[float, float]] = []
    hr_values: list[tuple[float, float]] = []
    cadence_values: list[tuple[float, float]] = []
    speed_values: list[tuple[float, float]] = []
    altitude_values: list[tuple[float, float]] = []

    first_ts: float | None = None

    # Session-level metrics
    duration_ms: float | None = None
    distance_m: float | None = None
    sport: str | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    calories: float | None = None
    avg_cadence: float | None = None
    max_cadence: float | None = None
    avg_power: float | None = None
    max_power: float | None = None
    power_meter: str | None = None

    def _get_field(frame, name):
        try:
            return frame.get_field(name)
        except KeyError:
            return None

    with fitdecode.FitReader(str(fit_path)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue

            # Extract power meter device info (optional)
            if extract_power_meter and frame.name == "device_info" and power_meter is None:
                is_power = False
                for f in frame.fields:
                    if f.name in ("antplus_device_type", "device_type", "local_device_type"):
                        if f.value == "bike_power" or (isinstance(f.value, int) and f.value == 12):
                            is_power = True
                if is_power:
                    mfr = next((f.value for f in frame.fields if f.name == "manufacturer"), None)
                    prod = next((f.value for f in frame.fields if f.name in ("garmin_product", "product")), None)
                    power_meter = f"{mfr}:{prod}"

            # Extract session-level metrics
            if frame.name == "session":
                ef = _get_field(frame, "total_elapsed_time")
                if ef is not None and ef.value is not None:
                    duration_ms = float(ef.value)

                ed = _get_field(frame, "total_distance")
                if ed is not None and ed.value is not None:
                    distance_m = float(ed.value)

                es = _get_field(frame, "sport")
                if es is not None and es.value is not None:
                    sport = str(es.value)

                eahr = _get_field(frame, "avg_heart_rate")
                if eahr is not None and eahr.value is not None:
                    avg_hr = float(eahr.value)

                emhr = _get_field(frame, "max_heart_rate")
                if emhr is not None and emhr.value is not None:
                    max_hr = float(emhr.value)

                ecal = _get_field(frame, "total_calories")
                if ecal is not None and ecal.value is not None:
                    calories = float(ecal.value)

                eac = _get_field(frame, "avg_cadence")
                if eac is not None and eac.value is not None:
                    avg_cadence = float(eac.value)

                emc = _get_field(frame, "max_cadence")
                if emc is not None and emc.value is not None:
                    max_cadence = float(emc.value)

                epwr_avg = _get_field(frame, "avg_power")
                if epwr_avg is not None and epwr_avg.value is not None:
                    avg_power = float(epwr_avg.value)

                epwr_max = _get_field(frame, "max_power")
                if epwr_max is not None and epwr_max.value is not None:
                    max_power = float(epwr_max.value)

            if frame.name != "record":
                continue

            ts_field = _get_field(frame, "timestamp")
            if ts_field is None or ts_field.value is None:
                continue
            ts = ts_field.value

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

    return FitParseResult(
        power_values=power_values,
        hr_values=hr_values,
        cadence_values=cadence_values,
        speed_values=speed_values,
        altitude_values=altitude_values,
        duration_ms=duration_ms,
        distance_m=distance_m,
        sport=sport,
        avg_hr=avg_hr,
        max_hr=max_hr,
        calories=calories,
        avg_cadence=avg_cadence,
        max_cadence=max_cadence,
        avg_power=avg_power,
        max_power=max_power,
        power_meter=power_meter,
    )


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
        return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


def _extract_floors(floors_data: dict[str, Any]) -> int | None:
    """Extract total floors ascended from Garmin floors data.

    floorValuesArray is a list of [startTime, endTime, floorsAscended, floorsDescended].
    """
    arr = floors_data.get("floorValuesArray")
    if not arr:
        return None
    total = 0
    for entry in arr:
        if isinstance(entry, (list, tuple)) and len(entry) >= 3:
            total += entry[2]
    return total if total > 0 else None


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
        min_interval: float = GARMIN_RATE_LIMIT_MIN_INTERVAL,
        max_backoff: float = GARMIN_RATE_LIMIT_MAX_BACKOFF,
        backoff_factor: float = GARMIN_RATE_LIMIT_BACKOFF_FACTOR,
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
        except Exception as e:
            # Guard against garminconnect module not being imported
            if garminconnect is None:
                raise RuntimeError(
                    "garminconnect module not available. "
                    "Install python-garminconnect."
                ) from e
            # Check if this is a 429 rate limit error
            error_cls = garminconnect.GarminConnectTooManyRequestsError
            if not isinstance(e, error_cls):
                raise
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
    from src.config import get_resolved_credential
    email = os.getenv("GARMIN_EMAIL", "")
    password = get_resolved_credential("GARMIN_PASSWORD")
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

    # Fallback: try vault/.garminconnect, then ~/.garminconnect
    if not tokenstore:
        vault = os.getenv("CYCLING_AGENT_VAULT", "")
        if vault and os.path.isdir(os.path.join(vault, ".garminconnect")):
            tokenstore = os.path.join(vault, ".garminconnect")
        elif os.path.isdir(os.path.expanduser("~/.garminconnect")):
            tokenstore = os.path.expanduser("~/.garminconnect")

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

    if result is None:
        raise RuntimeError(
            "Garmin login returned None — check credentials, network, "
            "or token directory permissions."
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
            logger.debug("Failed to fetch body composition", exc_info=True)
            body = None

        # Get weigh-ins (more reliable for weight)
        try:
            rl.wait()
            weigh_ins = _retry_on_rate_limit(
                lambda: client.get_daily_weigh_ins(date_str)
            )
        except Exception:
            logger.debug("Failed to fetch weigh-ins", exc_info=True)
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
            logger.debug("Failed to fetch sleep data", exc_info=True)


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

        # Parse FIT file using shared parser
        result = _parse_fit_frames(fit_path, extract_power_meter=True)
        if result is None:
            logger.warning("fitdecode not installed — cannot parse FIT files")
            return 0

        # Store power meter info in activities table
        if result.power_meter is not None:
            try:
                db.conn.execute(
                    "UPDATE activities SET power_meter = ? WHERE id = ?",
                    (result.power_meter, str(activity_id)),
                )
                db.conn.commit()
            except Exception as e:
                logger.warning(f"Failed to store power meter for {activity_id}: {e}")

        # Store raw FIT session metrics (immutable)
        try:
            db.store_raw_fit_session(activity_id, {
                "total_elapsed_time_ms": result.duration_ms,
                "total_distance_m": result.distance_m,
                "sport": result.sport,
                "avg_heart_rate": result.avg_hr,
                "max_heart_rate": result.max_hr,
                "total_calories": result.calories,
                "avg_cadence": result.avg_cadence,
                "max_cadence": result.max_cadence,
                "avg_power": result.avg_power,
                "max_power": result.max_power,
            })
        except Exception as e:
            logger.warning(f"Failed to store raw FIT session {activity_id}: {e}")

        if not any([result.power_values, result.hr_values, result.cadence_values, result.speed_values, result.altitude_values]):
            logger.debug(f"No stream data in FIT file for activity {activity_id}")
            return 0

        total_stored = 0
        for metric, values in [
            ("power", result.power_values),
            ("heart_rate", result.hr_values),
            ("cadence", result.cadence_values),
            ("speed", result.speed_values),
            ("altitude", result.altitude_values),
        ]:
            if values:
                if len(values) > 1:
                    seen: set[float] = set()
                    deduped: list[tuple[float, float]] = []
                    for t, v in values:
                        if t not in seen:
                            seen.add(t)
                            deduped.append((t, v))
                    values = deduped
                total_stored += db.store_activity_streams(str(activity_id), metric, values)
        logger.info(
            f"Parsed FIT for activity {activity_id}: "
            f"{len(result.power_values)} power, {len(result.hr_values)} HR, "
            f"{len(result.cadence_values)} cadence, {len(result.speed_values)} speed, "
            f"{len(result.altitude_values)} altitude samples"
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

    # Garmin API: duration is milliseconds, distance is meters
    # Convert duration to seconds for storage
    duration_ms = activity.get("duration") or 0
    distance_m = activity.get("distance") or 0

    return {
        "id": f"garmin_{activity_id}",
        "start_date_local": start_time_local,
        "type": activity_type,
        "duration": duration_ms / 1000.0,
        "distance": distance_m,
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
    batch_size = GARMIN_ACTIVITY_BATCH_SIZE
    offset = 0
    date_cutoff_reached = False

    # Try to get total count for progress estimation
    try:
        total_count = client.count_activities()
    except Exception:
        logger.debug("Failed to count activities", exc_info=True)
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

    # --- Phase 1.5: Store Raw API Data and Activity Summaries ---
    if new_activities:
        store_records = []
        for activity in new_activities:
            # Store raw Garmin API data (immutable)
            activity_id = activity.get("activityId")
            if activity_id is not None:
                try:
                    db.store_raw_activity(activity_id, activity)
                except Exception as e:
                    logger.warning(f"Failed to store raw activity {activity_id}: {e}")

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
            time.sleep(GARMIN_FIT_DOWNLOAD_INTERVAL)  # spread load between downloads
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

    # --- Phase 3: Rebuild activities table from raw data ---
    try:
        refreshed = db.refresh_activities()
        logger.info(f"Phase 3: Refreshed {refreshed} activities from raw data")
    except Exception as e:
        logger.warning(f"Failed to refresh activities: {e}")

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
            # Use the actual newest activity date, not today — avoids gaps
            newest = db.conn.execute("SELECT MAX(start_date) FROM activities").fetchone()[0]
            sync_date = newest[:10] if newest else today.isoformat()
            db.set_last_synced("garmin_activities", sync_date, resume_offset=0)
    except Exception:
        logger.debug("Failed to update last synced date", exc_info=True)

    try:
        db.close()
    except Exception:
        logger.debug("Failed to close database connection", exc_info=True)

    if progress_callback is not None:
        progress_callback(100, f"Sync complete: {total_processed} activities, {total_stored} records")

    return {
        "activities_processed": total_processed,
        "stream_records": total_stored,
    }


def extract_power_meters(db_path: str | None = None) -> int:
    """Extract power meter info from existing FIT files and store in activities table.

    Scans /data/raw/fit/ for FIT files, extracts device_info messages for
    power meters, and updates the activities table with the power_meter column.

    Returns count of activities updated.
    """
    import fitdecode
    from pathlib import Path

    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    db = CyclingDB(db_path)
    fit_dir = Path(config.vault_path()) / "raw" / "fit"
    if not fit_dir.exists():
        logger.warning(f"FIT directory not found: {fit_dir}")
        db.close()
        return 0

    updated = 0
    fit_files = sorted(fit_dir.glob("*.fit"))
    logger.info(f"Scanning {len(fit_files)} FIT files for power meter info...")

    for fit_path in fit_files:
        garmin_id = fit_path.stem
        activity_id = f"garmin_{garmin_id}"

        # Check if already has power_meter info
        row = db.conn.execute(
            "SELECT power_meter FROM activities WHERE id = ?",
            (activity_id,),
        ).fetchone()
        if row and row[0] is not None:
            continue

        try:
            with fitdecode.FitReader(str(fit_path)) as fit:
                for frame in fit:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue
                    if frame.name != "device_info":
                        continue
                    is_power = False
                    for f in frame.fields:
                        if f.name in ("antplus_device_type", "device_type", "local_device_type"):
                            if f.value == "bike_power" or (isinstance(f.value, int) and f.value == 12):
                                is_power = True
                    if not is_power:
                        continue
                    mfr = next((f.value for f in frame.fields if f.name == "manufacturer"), None)
                    prod = next((f.value for f in frame.fields if f.name in ("garmin_product", "product")), None)
                    pm = f"{mfr}:{prod}"
                    db.conn.execute(
                        "UPDATE activities SET power_meter = ? WHERE id = ?",
                        (pm, activity_id),
                    )
                    db.conn.commit()
                    updated += 1
                    break
        except Exception as e:
            logger.debug(f"Failed to parse {fit_path}: {e}")

    db.close()
    logger.info(f"Extracted power meter info for {updated} activities")
    return updated


def sync_garmin(
    days: int = 1,
    db_path: str | None = None,
    tokenstore: str | None = None,
    unbounded: bool = False,
    progress_callback: "Callable[[int, str], None] | None" = None,
    force_resync: bool = False,
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
    if force_resync:
        logger.info("Force resync: clearing existing wellness data")
        db.conn.execute("DELETE FROM wellness")
        db.conn.execute("DELETE FROM raw_wellness")
        db.conn.execute("DELETE FROM sync_state WHERE source='garmin_wellness'")
        db.conn.commit()
        last_synced = None
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
    current_date = last_date  # include today for partial data
    days_synced = 0
    # Cap unbounded sync to 10 years back to avoid datetime underflow
    max_days = MAX_SYNC_DAYS if unbounded else days
    while days_synced < max_days:
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

    # --- Narrow to days with actual watch data ---
    # Only fetch per-day endpoints for days that have weight or steps data from bulk fetch.
    # Days without any bulk data have no watch worn — skip them entirely.
    bulk_dates = set(weight_by_date.keys()) | set(steps_by_date.keys())

    # Also exclude days already in the DB
    existing_dates = db.get_wellness_dates(
        oldest=start_date.strftime("%Y-%m-%d"),
        newest=end_date.strftime("%Y-%m-%d"),
    )

    # Only sync dates that have bulk data AND are not already in DB
    fetch_dates = [
        d for d in sync_dates
        if d.strftime("%Y-%m-%d") in bulk_dates
        and d.strftime("%Y-%m-%d") not in existing_dates
    ]

    skipped_no_watch = len(sync_dates) - len([
        d for d in sync_dates if d.strftime("%Y-%m-%d") in bulk_dates
    ])
    skipped_existing = len(existing_dates & bulk_dates)

    if skipped_no_watch > 0:
        logger.info(
            f"Skipping {skipped_no_watch} days with no watch data (no weight/steps)"
        )
    if skipped_existing > 0:
        logger.info(f"Skipping {skipped_existing} days already in DB")
    logger.info(f"Fetching {len(fetch_dates)} days with watch data")

    total_stored = 0
    total_with_hrv = 0

    # Bulk fetch endurance/hill scores (date range)
    endurance_by_date: dict[str, float] = {}
    hill_by_date: dict[str, float] = {}
    try:
        _rate_limiter.wait()
        endurance_data = _retry_on_rate_limit(
            lambda: client.get_endurance_score(
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
            )
        )
        if isinstance(endurance_data, list):
            for entry in endurance_data:
                d = entry.get("calendarDate") or _safe_timestamp_to_date(entry.get("dateTimestamp"))
                if d:
                    endurance_by_date[d] = entry.get("enduranceScore")
    except Exception as e:
        logger.warning(f"Failed to bulk fetch endurance scores: {e}")
    try:
        _rate_limiter.wait()
        hill_data = _retry_on_rate_limit(
            lambda: client.get_hill_score(
                start_date.strftime("%Y-%m-%d"),
                end_date.strftime("%Y-%m-%d"),
            )
        )
        if isinstance(hill_data, list):
            for entry in hill_data:
                d = entry.get("calendarDate") or _safe_timestamp_to_date(entry.get("dateTimestamp"))
                if d:
                    hill_by_date[d] = entry.get("hillScore")
    except Exception as e:
        logger.warning(f"Failed to bulk fetch hill scores: {e}")

    if progress_callback is not None:
        progress_callback(10, "Bulk data fetched, now fetching per-day data...")

    for i, d in enumerate(fetch_dates):
        target_str = d.strftime("%Y-%m-%d")

        # --- Fetch all per-day endpoints ---
        rmssd = None
        sleep_score = None
        sleep_hours = None
        resting_hr = None
        stress = None
        spo2 = None
        respiration_rate = None
        hydration_ml = None
        intensity_minutes = None
        body_battery = None
        body_battery_start = None
        body_battery_end = None
        floors = None
        training_readiness_score = None
        calories = None
        active_calories = None
        distance_m = None
        min_hr = None
        max_hr = None

        # HRV
        try:
            _rate_limiter.wait()
            hrv_data = _retry_on_rate_limit(lambda: client.get_hrv_data(target_str))
            if hrv_data:
                db.store_raw_wellness(target_str, "hrv", hrv_data)
                if "hrvSummary" in hrv_data:
                    rmssd = hrv_data["hrvSummary"].get("lastNightAvg")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during HRV sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch HRV for {target_str}: {e}")

        # Sleep
        try:
            _rate_limiter.wait()
            sleep_data = _retry_on_rate_limit(lambda: client.get_sleep_data(target_str))
            if sleep_data:
                db.store_raw_wellness(target_str, "sleep", sleep_data)
                daily = sleep_data.get("dailySleepDTO", {})
                scores = daily.get("sleepScores", {})
                overall = scores.get("overall", {})
                sleep_score = overall.get("value")
                sleep_ms = daily.get("sleepTimeSeconds", 0)
                if sleep_ms:
                    sleep_hours = sleep_ms / 3600.0
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during sleep sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch sleep for {target_str}: {e}")

        # Stats (RHR, stress)
        try:
            _rate_limiter.wait()
            stats = _retry_on_rate_limit(lambda: client.get_stats(target_str))
            if stats:
                db.store_raw_wellness(target_str, "stats", stats)
                resting_hr = stats.get("restingHeartRate")
                if stats.get("allDayStress"):
                    stress = stats["allDayStress"].get("averageStressLevel")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during stats sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch stats for {target_str}: {e}")

        # Heart rates (for min/max)
        try:
            _rate_limiter.wait()
            heart_rates = _retry_on_rate_limit(lambda: client.get_heart_rates(target_str))
            if heart_rates:
                db.store_raw_wellness(target_str, "heart_rates", heart_rates)
                resting_hr = resting_hr or heart_rates.get("restingHeartRate")
                min_hr = heart_rates.get("minHeartRate")
                max_hr = heart_rates.get("maxHeartRate")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during heart rates sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch heart rates for {target_str}: {e}")

        # Respiration
        try:
            _rate_limiter.wait()
            resp_data = _retry_on_rate_limit(lambda: client.get_respiration_data(target_str))
            if resp_data:
                db.store_raw_wellness(target_str, "respiration", resp_data)
                respiration_rate = resp_data.get("avgSleepRespirationValue")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during respiration sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch respiration for {target_str}: {e}")

        # SpO2
        try:
            _rate_limiter.wait()
            spo2_data = _retry_on_rate_limit(lambda: client.get_spo2_data(target_str))
            if spo2_data:
                db.store_raw_wellness(target_str, "spo2", spo2_data)
                spo2 = spo2_data.get("averageSpO2")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during SpO2 sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch SpO2 for {target_str}: {e}")

        # Hydration
        try:
            _rate_limiter.wait()
            hydr_data = _retry_on_rate_limit(lambda: client.get_hydration_data(target_str))
            if hydr_data:
                db.store_raw_wellness(target_str, "hydration", hydr_data)
                hydration_ml = hydr_data.get("valueInML")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during hydration sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch hydration for {target_str}: {e}")

        # Intensity minutes
        try:
            _rate_limiter.wait()
            intensity_data = _retry_on_rate_limit(lambda: client.get_intensity_minutes_data(target_str))
            if intensity_data:
                db.store_raw_wellness(target_str, "intensity_minutes", intensity_data)
                intensity_minutes = intensity_data.get("moderateMinutes", 0) + intensity_data.get("vigorousMinutes", 0)
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during intensity minutes sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch intensity minutes for {target_str}: {e}")

        # Body battery
        try:
            _rate_limiter.wait()
            bb_data = _retry_on_rate_limit(lambda: client.get_body_battery(target_str))
            if bb_data:
                db.store_raw_wellness(target_str, "body_battery", bb_data)
                if isinstance(bb_data, list) and len(bb_data) > 0:
                    arr = bb_data[0].get("bodyBatteryValuesArray", [])
                    if arr:
                        body_battery_start = arr[0][1]
                        body_battery_end = arr[-1][1]
                        body_battery = body_battery_end
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during body battery sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch body battery for {target_str}: {e}")

        # Floors climbed
        try:
            _rate_limiter.wait()
            floors_data = _retry_on_rate_limit(lambda: client.get_floors(target_str))
            if floors_data:
                db.store_raw_wellness(target_str, "floors", floors_data)
                floors = _extract_floors(floors_data)
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during floors sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch floors for {target_str}: {e}")

        # Training readiness
        try:
            _rate_limiter.wait()
            readiness_data = _retry_on_rate_limit(lambda: client.get_training_readiness(target_str))
            if readiness_data:
                db.store_raw_wellness(target_str, "training_readiness", readiness_data)
                if isinstance(readiness_data, list) and len(readiness_data) > 0:
                    training_readiness_score = readiness_data[0].get("score")
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during training readiness sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch training readiness for {target_str}: {e}")

        # User summary (calories, active_calories, distance)
        try:
            _rate_limiter.wait()
            summary = _retry_on_rate_limit(lambda: client.get_user_summary(target_str))
            if summary:
                db.store_raw_wellness(target_str, "user_summary", summary)
                calories = summary.get("totalKilocalories")
                active_calories = summary.get("activeKilocalories")
                dist_m = summary.get("totalDistanceMeters")
                if dist_m:
                    distance_m = dist_m
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during user summary sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch user summary for {target_str}: {e}")

        # Lifestyle logging
        try:
            _rate_limiter.wait()
            lifestyle = _retry_on_rate_limit(lambda: client.get_lifestyle_logging_data(target_str))
            if lifestyle:
                db.store_raw_wellness(target_str, "lifestyle", lifestyle)
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during lifestyle sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch lifestyle for {target_str}: {e}")

        # Morning training readiness
        try:
            _rate_limiter.wait()
            morning_readiness = _retry_on_rate_limit(lambda: client.get_morning_training_readiness(target_str))
            if morning_readiness:
                db.store_raw_wellness(target_str, "morning_readiness", morning_readiness)
        except garminconnect.GarminConnectTooManyRequestsError as e:
            _rate_limiter.record_429()
            logger.warning(f"Rate limited during morning readiness sync for {target_str}: {e}")
            # Do NOT advance last_synced — this day will be retried on next sync
            break
        except Exception as e:
            logger.debug(f"Failed to fetch morning readiness for {target_str}: {e}")

        # Build wellness record from all sources
        weight = weight_by_date.get(target_str)
        steps = steps_by_date.get(target_str)
        endurance_score = endurance_by_date.get(target_str)
        hill_score = hill_by_date.get(target_str)

        if not any([resting_hr, rmssd, stress, steps, weight, spo2, respiration_rate,
                     hydration_ml, intensity_minutes, body_battery, floors,
                     training_readiness_score, sleep_score, calories]):
            logger.info(f"No wellness data for {target_str}")
            db.set_last_synced("garmin_wellness", target_str)
            if progress_callback is not None:
                pct = 10 + int(i / max(len(fetch_dates), 1) * 80)
                progress_callback(min(pct, 95), f"Wellness: {target_str} ({i+1}/{len(fetch_dates)} days)")
            time.sleep(GARMIN_WELLNESS_POLL_INTERVAL)
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
            "spo2": spo2,
            "respiration_rate": respiration_rate,
            "floors": floors,
            "hydration_ml": hydration_ml,
            "intensity_minutes": intensity_minutes,
            "body_battery": body_battery,
            "body_battery_start": body_battery_start,
            "body_battery_end": body_battery_end,
            "training_readiness_score": training_readiness_score,
            "calories": calories,
            "active_calories": active_calories,
            "distance_m": distance_m,
            "min_hr": min_hr,
            "max_hr": max_hr,
            "endurance_score": endurance_score,
            "hill_score": hill_score,
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
            pct = 10 + int(i / max(len(fetch_dates), 1) * 80)
            progress_callback(min(pct, 95), f"Wellness: {target_str} ({i+1}/{len(fetch_dates)} days)")

        time.sleep(GARMIN_WELLNESS_POLL_INTERVAL)

    db.close()
    return {
        "wellness_records": total_stored,
        "with_hrv": total_with_hrv,
    }


def reparse_all_fit_files(
    db_path: str | None = None,
    progress_callback: "Callable[[int, str], None] | None" = None,
) -> dict[str, int]:
    """
    Delete all activity streams and re-parse every local FIT file.

    This is useful when the stream parsing logic has changed (e.g. dedup
    fixes, NP formula changes) and you want to re-derive all metrics from
    the already-downloaded FIT files without re-downloading from Garmin.

    Returns dict with counts of activities processed and stream records stored.
    """
    if fitdecode is None:
        raise RuntimeError("fitdecode not installed — cannot parse FIT files")

    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    db = CyclingDB(db_path)

    # Step 1: Delete all existing activity streams
    logger.info("Deleting all existing activity streams...")
    db.conn.execute("DELETE FROM activity_streams")
    db.conn.commit()

    # Step 2: Find all local FIT files
    vault = config.vault_path()
    fit_dir = vault / "raw" / "fit"
    if not fit_dir.exists():
        logger.warning(f"FIT directory does not exist: {fit_dir}")
        db.close()
        return {"activities_processed": 0, "stream_records": 0}

    fit_files = sorted(fit_dir.glob("*.fit"))
    logger.info(f"Found {len(fit_files)} local FIT files to re-parse")

    if progress_callback is not None:
        progress_callback(0, f"Found {len(fit_files)} FIT files to re-parse")

    # Step 3: Parse each FIT file
    total_processed = 0
    total_stored = 0

    for i, fit_path in enumerate(fit_files):
        activity_id = int(fit_path.stem)

        try:
            stored = _parse_fit_file(activity_id, fit_path, db)
            total_processed += 1
            total_stored += stored
        except Exception as e:
            logger.warning(f"Failed to re-parse {fit_path.name}: {type(e).__name__}: {e}")
            total_processed += 1

        if progress_callback is not None:
            pct = int(i / max(len(fit_files), 1) * 95)
            progress_callback(
                pct,
                f"Re-parsing FIT: {fit_path.name} ({i + 1}/{len(fit_files)})",
            )

    # Rebuild activities table from raw data
    try:
        refreshed = db.refresh_activities()
        logger.info(f"Refreshed {refreshed} activities after re-parse")
    except Exception as e:
        logger.warning(f"Failed to refresh activities after re-parse: {e}")

    db.close()

    if progress_callback is not None:
        progress_callback(100, f"Re-parsed {total_processed} activities, {total_stored} records")

    logger.info(
        f"Re-parse complete: {total_processed} activities processed, "
        f"{total_stored} stream records stored"
    )

    return {
        "activities_processed": total_processed,
        "stream_records": total_stored,
    }


def _parse_fit_file(
    activity_id: int,
    fit_path: "Path",
    db: CyclingDB,
) -> int:
    """Parse a single FIT file and store streams into the DB.

    Extracts session-level metrics from the FIT file and stores them in
    raw_fit_sessions (immutable). The activities table is rebuilt by
    refresh_activities() from raw_activities + raw_fit_sessions + activity_metrics.
    """
    result = _parse_fit_frames(fit_path, extract_power_meter=False)
    if result is None:
        logger.warning("fitdecode not installed — cannot parse FIT files")
        return 0

    # Store raw FIT session metrics (immutable, append-only)
    try:
        db.store_raw_fit_session(activity_id, {
            "total_elapsed_time_ms": result.duration_ms,
            "total_distance_m": result.distance_m,
            "sport": result.sport,
            "avg_heart_rate": result.avg_hr,
            "max_heart_rate": result.max_hr,
            "total_calories": result.calories,
            "avg_cadence": result.avg_cadence,
            "max_cadence": result.max_cadence,
            "avg_power": result.avg_power,
            "max_power": result.max_power,
        })
    except Exception as e:
        logger.warning(f"Failed to store raw FIT session {activity_id}: {e}")

    if not any([result.power_values, result.hr_values, result.cadence_values, result.speed_values, result.altitude_values]):
        logger.debug(f"No stream data in FIT file for activity {activity_id}")
        return 0

    total_stored = 0
    for metric, values in [
        ("power", result.power_values),
        ("heart_rate", result.hr_values),
        ("cadence", result.cadence_values),
        ("speed", result.speed_values),
        ("altitude", result.altitude_values),
    ]:
        if values:
            if len(values) > 1:
                seen: set[float] = set()
                deduped: list[tuple[float, float]] = []
                for t, v in values:
                    if t not in seen:
                        seen.add(t)
                        deduped.append((t, v))
                values = deduped
            total_stored += db.store_activity_streams(str(activity_id), metric, values)

    logger.info(
        f"Parsed FIT for activity {activity_id}: "
        f"{len(result.power_values)} power, {len(result.hr_values)} HR, "
        f"{len(result.cadence_values)} cadence, {len(result.speed_values)} speed, "
        f"{len(result.altitude_values)} altitude samples"
    )
    return total_stored

if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config.setup()

    days = DEFAULT_CLI_SYNC_DAYS
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

    # Build list of dates to sync (going backwards from yesterday)
    sync_dates: list[date] = []
    current_date = last_date - timedelta(days=1)
    for _ in range(days):
        sync_dates.append(current_date)
        current_date -= timedelta(days=1)

    # Only fetch days that don't already have wellness records
    existing_dates = db.get_wellness_dates(
        oldest=sync_dates[-1].strftime("%Y-%m-%d"),
        newest=sync_dates[0].strftime("%Y-%m-%d"),
    )
    missing_dates = [d for d in sync_dates if d.strftime("%Y-%m-%d") not in existing_dates]

    if existing_dates:
        logger.info(
            f"Skipping {len(existing_dates)} days with existing data, "
            f"fetching {len(missing_dates)} missing days"
        )

    total_stored = 0
    total_with_hrv = 0

    for d in missing_dates:
        target_str = d.strftime("%Y-%m-%d")
        logger.info(f"Syncing wellness for {target_str}")
        record = fetch_wellness_for_date(client, target_str)
        if record is None:
            logger.info(f"No wellness data for {target_str}")
        else:
            stored = db.store_wellness([record])
            with_hrv = 1 if record.get("rmssd") is not None else 0
            total_stored += stored
            total_with_hrv += with_hrv
        db.set_last_synced("garmin_wellness", target_str)
        time.sleep(GARMIN_WELLNESS_POLL_INTERVAL)

    db.close()
    print(f"Done. Wellness: {total_stored}, With HRV: {total_with_hrv}")