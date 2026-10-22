"""
Shared Garmin ingestion pipeline: FIT parsing, rate limiting, and the
batch activity-sync used by the Personal API backend (garmin_personal.py).

Activity discovery + FIT download run through a small client interface
(``get_activities`` / ``count_activities`` / ``download_activity``) so the
batch pipeline stays decoupled from the API client.
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from src import config
from src.config.constants import (
    GARMIN_RATE_LIMIT_MIN_INTERVAL,
    GARMIN_RATE_LIMIT_MAX_BACKOFF,
    GARMIN_RATE_LIMIT_BACKOFF_FACTOR,
    GARMIN_ACTIVITY_BATCH_SIZE,
    GARMIN_FIT_DOWNLOAD_INTERVAL,
)
from src.db.store import CyclingDB

logger = logging.getLogger(__name__)

try:
    import fitdecode
except ImportError:
    fitdecode = None  # type: ignore


# ── API error taxonomy ────────────────────────────────────────────────
# Defined here (not in garmin_personal) so the pipeline can catch them
# without a circular import; garmin_personal re-exports them.

class GarminPersonalAuthError(RuntimeError):
    """The Personal API key is missing, invalid, or revoked (401/403)."""


class GarminPersonalRateLimited(RuntimeError):
    """A Personal API request was rate limited (429)."""


class GarminPersonalApiError(RuntimeError):
    """A Personal API request failed (non-auth, non-rate-limit)."""


# ── FIT parsing ───────────────────────────────────────────────────────

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
                    # FIT total_elapsed_time is in seconds — stored as-is in total_elapsed_time_ms column
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


# ── Rate limiting ─────────────────────────────────────────────────────

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
        except GarminPersonalRateLimited:
            _rate_limiter.record_429()
            if attempt == max_retries:
                logger.warning(
                    f"Rate limit retry exhausted after {max_retries} attempts"
                )
                raise
            wait = min(_rate_limiter._effective_interval, _rate_limiter.max_backoff)
            logger.info(
                f"Rate limited (attempt {attempt}/{max_retries}), "
                f"retrying in {wait:.1f}s..."
            )
            time.sleep(wait)


# ── Activity stream download + storage ───────────────────────────────

def _fetch_activity_streams(
    client,
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
                lambda: client.download_activity(str(activity_id))
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

        # Store power meter info in activities table (use garmin_ prefix to match activities table)
        if result.power_meter is not None:
            try:
                db._exec(
                    "UPDATE activities SET power_meter = ? WHERE id = ?",
                    (result.power_meter, f"garmin_{activity_id}"),
                )
                db._commit()
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
                total_stored += db.store_activity_streams(f"garmin_{activity_id}", metric, values)
        logger.info(
            f"Parsed FIT for activity {activity_id}: "
            f"{len(result.power_values)} power, {len(result.hr_values)} HR, "
            f"{len(result.cadence_values)} cadence, {len(result.speed_values)} speed, "
            f"{len(result.altitude_values)} altitude samples"
        )
        return total_stored

    except GarminPersonalRateLimited:
        # Propagate to the batch handler: it saves the resume offset and
        # aborts. Swallowing 429s here would silently skip every remaining
        # activity's streams and mark the batch complete.
        raise
    except Exception as e:
        logger.warning(f"Failed to fetch streams for activity {activity_id}: {type(e).__name__}: {e}")
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
    # Garmin API: duration is seconds, distance is meters
    duration_s = activity.get("duration") or 0
    distance_m = activity.get("distance") or 0

    return {
        "id": f"garmin_{activity_id}",
        "start_date_local": start_time_local,
        "type": activity_type,
        "duration": duration_s,
        "max_hr": activity.get("maxHeartRate"),
        "calories": activity.get("calories"),
        "tss": activity.get("trainingStressScore"),
        "ifr": None,
        "normalized_power": activity.get("normPower"),
        "file_type": None,
    }


def _sync_activities_batch(
    client,
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
        except GarminPersonalRateLimited as e:
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
        except GarminPersonalRateLimited as e:
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


# ── Power meter extraction (local FIT files) ──────────────────────────

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
        row = db._exec(
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
                    db._exec(
                        "UPDATE activities SET power_meter = ? WHERE id = ?",
                        (pm, activity_id),
                    )
                    db._commit()
                    updated += 1
                    break
        except Exception as e:
            logger.warning(f"Failed to parse {fit_path}: {e}")

    db.close()
    logger.info(f"Extracted power meter info for {updated} activities")
    return updated


# ── FIT reparse (local files, no network) ─────────────────────────────

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
    db._exec("DELETE FROM activity_streams")
    db._commit()

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
            total_stored += db.store_activity_streams(f"garmin_{activity_id}", metric, values)

    logger.info(
        f"Parsed FIT for activity {activity_id}: "
        f"{len(result.power_values)} power, {len(result.hr_values)} HR, "
        f"{len(result.cadence_values)} cadence, {len(result.speed_values)} speed, "
        f"{len(result.altitude_values)} altitude samples"
    )
    return total_stored
