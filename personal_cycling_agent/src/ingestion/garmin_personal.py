"""
Garmin Connect Personal API client + sync (the only data source).

Official first-party access to the user's own Garmin Connect data using a
Personal API key (created in the Garmin Connect web UI).

Auth model:
    1. POST the Personal API key to the token endpoint -> a Bearer token.
    2. The Bearer token is valid 24h and has NO refresh token; a new one is
       obtained from the key whenever the cached one is expired/revoked.

The token is cached in the vault (``.garmin_personal_token.json``) so the
background scheduler does not hit the token endpoint on every poll.

Endpoints used (base: https://personal.connectapi.garmin.com):
    - /personalapitoken-service/personal-api/personal-api-key/token   (POST)
    - /usersummary-service/v2/dailysummaries          (kitchen sink: steps,
      floors, calories, HR, intensity minutes, stress, body battery,
      hydration, respiration, SpO2)
    - /hrv-service/hrv/{date}                         (RMSSD)
    - /sleep-service/sleep/dailySleep?date=           (sleep score/hours)
    - /wellness-service/wellness/bodyBattery/reports/daily
    - /weight-service/weight/range/{s}/{e}            (weight + body comp)
    - /activitylist-service/activities/search/activities
    - /download-service/files/activity/{id}           (original .fit as ZIP)

NOTE on the FIT download: the server requires ``Accept`` to include
``application/x-zip-compressed`` (or ``*/*``). Sending only ``application/zip``
returns 406 Not Acceptable.
"""

import base64
import io
import json
import logging
import os
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import requests

from src import config as _config_mod

from src.config.constants import (
    MAX_SYNC_DAYS,
    GARMIN_WELLNESS_POLL_INTERVAL,
)
from src.db.store import CyclingDB
from src.ingestion.garmin_pipeline import (
    GarminPersonalApiError,
    GarminPersonalAuthError,
    GarminPersonalRateLimited,
    _retry_on_rate_limit,
    _sync_activities_batch,
    rate_limiter,
    reset_rate_limiter,
)

__all__ = [
    "PersonalApi",
    "PersonalActivityClient",
    "GarminPersonalApiError",
    "GarminPersonalAuthError",
    "GarminPersonalRateLimited",
    "test_personal_connection",
    "sync_garmin",
    "sync_activities",
]

logger = logging.getLogger(__name__)

try:
    import fitdecode
except ImportError:
    fitdecode = None  # type: ignore

BASE_URL = os.environ.get("GARMIN_PERSONAL_API_BASE_URL", "https://personal.connectapi.garmin.com").rstrip("/")
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"

# The download-service rejects an Accept header that lacks x-zip-compressed.
FIT_ACCEPT = "application/zip, application/x-zip-compressed, application/octet-stream"

# Re-fetch the token when it expires within this window.
TOKEN_REFRESH_WINDOW_S = 60 * 60

# The activity-search endpoint 500s for limit > 50.
PERSONAL_MAX_ACTIVITY_LIMIT = 50


def _token_cache_path() -> Path:
    return _config_mod.vault_path() / ".garmin_personal_token.json"


def get_personal_api_key() -> str:
    """Return the configured Personal API key, or '' if unset."""
    return os.environ.get("GARMIN_PERSONAL_API_KEY", "").strip()


def _jwt_exp(token: str) -> float:
    """Return the JWT ``exp`` claim (epoch seconds) without verifying the sig."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return 0.0


class PersonalApi:
    """Thin authenticated client for the Garmin Connect Personal API."""

    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        self.api_key = (api_key if api_key is not None else get_personal_api_key()).strip()
        if not self.api_key:
            raise GarminPersonalAuthError(
                "GARMIN_PERSONAL_API_KEY is not set. Create a Personal API key in "
                "the Garmin Connect web UI and add it to config.env."
            )
        self.timeout = timeout
        self.session = requests.Session()
        self._token = ""
        self._token_exp = 0.0

    # ── token management ─────────────────────────────────────────────
    def _load_cached_token(self) -> None:
        try:
            cache = json.loads(_token_cache_path().read_text(encoding="utf-8"))
            if cache.get("key") == self.api_key and cache.get("token"):
                self._token = cache["token"]
                self._token_exp = float(cache.get("exp", 0))
        except (FileNotFoundError, ValueError, KeyError):
            pass

    def _save_cached_token(self) -> None:
        try:
            path = _token_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"key": self.api_key, "token": self._token, "exp": self._token_exp}, f)
        except OSError as e:
            logger.warning("Could not cache Personal API token: %s", e)

    def _fetch_token(self) -> None:
        resp = self.session.post(
            f"{BASE_URL}{TOKEN_PATH}",
            headers={"Authorization": f"ApiKey {self.api_key}", "Accept": "*/*"},
            timeout=self.timeout,
        )
        if resp.status_code in (401, 403):
            raise GarminPersonalAuthError(
                f"Personal API key rejected ({resp.status_code}). The key may be "
                "expired or revoked — create a new one in the Garmin Connect web UI."
            )
        if resp.status_code != 200:
            raise GarminPersonalAuthError(f"Token endpoint returned {resp.status_code}: {resp.text[:200]}")
        self._token = resp.text.strip()
        self._token_exp = _jwt_exp(self._token)
        self._save_cached_token()

    def _ensure_token(self, force: bool = False) -> None:
        if not force:
            self._load_cached_token()
            if self._token and (self._token_exp - time.time()) > TOKEN_REFRESH_WINDOW_S:
                return
        self._fetch_token()

    # ── request helper ───────────────────────────────────────────────
    def request(self, method: str, path: str, params: dict | None = None,
                accept: str = "application/json") -> Any:
        """Authenticated request; transparently re-fetches the token once on 401.
        Returns parsed JSON (or bytes when accept is not JSON)."""
        self._ensure_token()
        resp = self._do(method, path, params, accept)
        if resp.status_code == 401:
            self._ensure_token(force=True)
            resp = self._do(method, path, params, accept)
        if resp.status_code == 429:
            raise GarminPersonalRateLimited(f"Rate limited on {path}")
        if resp.status_code in (401, 403):
            raise GarminPersonalAuthError(
                f"Personal API auth failed ({resp.status_code}) on {path}. "
                "The key may be expired or revoked."
            )
        if not resp.ok:
            raise GarminPersonalApiError(f"{method} {path} -> {resp.status_code}: {resp.text[:200]}")
        if accept == "application/json":
            return resp.json() if resp.content else None
        return resp.content

    def _do(self, method: str, path: str, params: dict | None, accept: str) -> "requests.Response":
        return self.session.request(
            method,
            f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {self._token}", "Accept": accept},
            params=params,
            timeout=self.timeout,
        )

    # ── wellness ─────────────────────────────────────────────────────
    def daily_summaries(self, start: str, end: str) -> dict[str, dict]:
        """Return {date: summary} for an inclusive range (no 7-day cap)."""
        data = self.request("GET", "/usersummary-service/v2/dailysummaries",
                            {"startDate": start, "endDate": end})
        out: dict[str, dict] = {}
        for item in (data or {}).get("data", []) if isinstance(data, dict) else []:
            d = item.get("calendarDate")
            if d:
                out[d] = item
        return out

    def hrv(self, day: str) -> dict | None:
        return self.request("GET", f"/hrv-service/hrv/{day}")

    def sleep(self, day: str) -> dict | None:
        return self.request("GET", "/sleep-service/sleep/dailySleep", {"date": day})

    def body_battery(self, day: str) -> list:
        data = self.request("GET", "/wellness-service/wellness/bodyBattery/reports/daily",
                            {"startDate": day, "endDate": day})
        return data if isinstance(data, list) else []

    def weight_range(self, start: str, end: str) -> dict:
        data = self.request("GET", f"/weight-service/weight/range/{start}/{end}", {"includeAll": "true"})
        return data if isinstance(data, dict) else {}

    def get_user_profile(self) -> dict:
        """Fetch the user's profile settings (displayName, etc.)."""
        data = self.request("GET", "/userprofile-service/userprofile/settings")
        return data if isinstance(data, dict) else {}

    # ── activities ───────────────────────────────────────────────────
    def search_activities(self, start: int = 0, limit: int = 100,
                          start_date: str | None = None, end_date: str | None = None) -> list:
        params: dict[str, Any] = {"start": start, "limit": limit}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        data = self.request("GET", "/activitylist-service/activities/search/activities", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("activityList", "activities", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
        return []

    def download_fit(self, activity_id: int | str) -> bytes:
        return self.request("GET", f"/download-service/files/activity/{activity_id}", accept=FIT_ACCEPT)


# ── field extraction (Personal API -> the app's normalized fields) ────

def _get(d: Any, *path: str, default=None):
    """Safely walk a nested dict by keys; return default if any step is missing."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def extract_wellness_day(summary: dict | None, hrv: dict | None,
                         sleep: dict | None, body_battery: list) -> dict[str, Any]:
    """Extract the app's normalized wellness fields from Personal API responses.

    Returns a dict with the same keys the wellness sync writes into the
    record (minus weight/steps, which come from the bulk fetch).
    """
    summary = summary or {}
    hrv = hrv or {}
    sleep = sleep or {}

    rmssd = _get(hrv, "hrvSummary", "lastNightAvg")
    sleep_score = _get(sleep, "sleepScores", "overall", "value")
    sleep_ms = sleep.get("sleepTimeSeconds")
    sleep_hours = (sleep_ms / 3600.0) if sleep_ms else None

    body_battery_start = body_battery_end = None
    if isinstance(body_battery, list) and body_battery:
        # Per-day report: list of objects, each with a bodyBatteryValuesArray
        # of [timestamp, value] pairs (mirrors the legacy extraction).
        report = body_battery[0] if isinstance(body_battery[0], dict) else {}
        arr = report.get("bodyBatteryValuesArray") or []
        if arr:
            try:
                body_battery_start = arr[0][1]
                body_battery_end = arr[-1][1]
                body_battery = body_battery_end
            except (IndexError, TypeError):
                pass
    else:
        # Fall back to the daily summary's latest value when no per-day
        # body-battery report was fetched.
        body_battery = _get(summary, "bodyBattery", "latestValue")

    intensity = (
        (_get(summary, "intensityMinutes", "moderate", default=0) or 0)
        + (_get(summary, "intensityMinutes", "vigorous", default=0) or 0)
    )

    return {
        "rmssd": rmssd,
        "resting_hr": _get(summary, "heartRate", "restingValue"),
        "stress": _get(summary, "stress", "avgLevel"),
        "sleep_score": sleep_score,
        "sleep_hours": sleep_hours,
        "spo2": _get(summary, "pulseOx", "avgValue"),
        "respiration_rate": _get(summary, "respiration", "avgValue"),
        "hydration_ml": _get(summary, "hydration", "consumedInMl"),
        "intensity_minutes": intensity or None,
        "body_battery": body_battery,
        "body_battery_start": body_battery_start,
        "body_battery_end": body_battery_end,
        "floors": _get(summary, "movement", "floorsAscended", "value"),
        "training_readiness_score": None,  # not exposed by the Personal API
        "calories": _get(summary, "calories", "burnedTotal"),
        "active_calories": _get(summary, "calories", "burnedActive"),
        "distance_m": _get(summary, "movement", "steps", "distanceInMeters"),
        "min_hr": _get(summary, "heartRate", "minValue"),
        "max_hr": _get(summary, "heartRate", "maxValue"),
    }


def extract_weight(weight_range: dict) -> dict[str, float]:
    """Return {date: weight_kg} from the Personal API weight-range response."""
    out: dict[str, float] = {}
    for entry in (weight_range or {}).get("dailyWeightSummaries", []) or []:
        d = entry.get("summaryDate")
        w = entry.get("latestWeight") or entry.get("maxWeight")
        if d and w:
            out[d] = float(w)
    return out


def extract_steps(summary: dict | None) -> int | None:
    """Return total steps from a daily-summaries movement block."""
    return _get(summary, "movement", "steps", "value")


# ── client adapter (feeds the shared activity pipeline) ──────────────

class PersonalActivityClient:
    """Presents the Personal API as the small client surface
    that ``_sync_activities_batch`` / ``_fetch_activity_streams`` rely on:
    ``get_activities``, ``count_activities``, ``download_activity``.

    This lets the shared activity-sync pipeline run unchanged against
    the Personal API. The activity-search and FIT-download response shapes
    match what those functions expect.
    """

    def __init__(self, api: PersonalApi):
        self._api = api

    def get_activities(self, start: int = 0, limit: int = 100) -> list:
        """Return up to *limit* activities starting at *start*.

        The search endpoint 500s for limit > 50, so requests larger than
        PERSONAL_MAX_ACTIVITY_LIMIT are served by multiple API calls. This
        keeps the batch pipeline's page-size math (which assumes it receives
        exactly the requested limit until the list ends) working unchanged.
        """
        out: list = []
        offset = start
        remaining = limit
        while remaining > 0:
            page = self._api.search_activities(
                start=offset, limit=min(remaining, PERSONAL_MAX_ACTIVITY_LIMIT)
            )
            out.extend(page)
            remaining -= len(page)
            offset += len(page)
            if len(page) < PERSONAL_MAX_ACTIVITY_LIMIT:
                break  # end of the activity list
        return out

    def count_activities(self) -> int:
        # No count endpoint on the Personal API; the caller treats failure as
        # "unknown total" (progress estimation only).
        raise GarminPersonalApiError("count not supported by Personal API")

    def download_activity(self, activity_id, download_format=None) -> bytes:
        return self._api.download_fit(activity_id)


def test_personal_connection(api_key: str) -> tuple[bool, str]:
    """Test a Personal API key by fetching a token and the user profile.

    Returns a ``(ok, message)`` tuple for the Settings UI.
    """
    if not api_key or not api_key.strip():
        return False, "No API key provided."
    try:
        api = PersonalApi(api_key=api_key.strip())
        api._ensure_token()
        profile = api.get_user_profile()
        name = (
            profile.get("fullName")
            or profile.get("displayName")
            or "your Garmin account"
        )
        return True, f"Connected to {name}."
    except GarminPersonalAuthError as e:
        return False, f"Authentication failed: {e}"
    except GarminPersonalRateLimited as e:
        return False, f"Rate limited: {e}"
    except GarminPersonalApiError as e:
        return False, f"API error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


# ── wellness sync (Personal API) ──────────────────────────────────────

def _compute_sync_dates(last_synced: str | None, today: date, days: int,
                        unbounded: bool) -> list[date]:
    """Return the wellness dates to sync (empty if already up to date).

    Mirrors the original ``sync_garmin`` date logic so the wellness sync
    advances the same cursor:
    """
    if unbounded:
        if last_synced:
            try:
                last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
            except ValueError:
                last_date = today - timedelta(days=1)
            start = last_date + timedelta(days=1)
            if start > today:
                return []
            out, d = [], start
            while d <= today:
                out.append(d)
                d += timedelta(days=1)
            return out
        current = today - timedelta(days=1)
        out = []
        for _ in range(MAX_SYNC_DAYS):
            out.append(current)
            current -= timedelta(days=1)
        out.reverse()
        return out
    if last_synced:
        try:
            last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
        except ValueError:
            last_date = today - timedelta(days=1)
    else:
        last_date = today - timedelta(days=1)
    start = last_date + timedelta(days=1)
    if start > today:
        return []
    all_dates, d = [], start
    while d <= today:
        all_dates.append(d)
        d += timedelta(days=1)
    return all_dates[:days] if days < len(all_dates) else all_dates


def sync_garmin(
    days: int = 1,
    db_path: str | None = None,
    unbounded: bool = False,
    progress_callback: Callable[[int, str], None] | None = None,
    force_resync: bool = False,
) -> dict[str, int]:
    """Sync wellness data from the Garmin Connect Personal API."""
    if db_path is None:
        db_path = str(_config_mod.db_path("cycling_agent.sqlite"))
    if not get_personal_api_key():
        raise RuntimeError(
            "GARMIN_PERSONAL_API_KEY not set. Create a Personal API key in the "
            "Garmin Connect web UI and add it to config.env."
        )

    # Shared rate limiter and retry helper come from garmin_pipeline.

    db = CyclingDB(db_path)
    if force_resync:
        logger.info("[personal] Force resync: clearing existing wellness data")
        db._exec("DELETE FROM wellness")
        db._exec("DELETE FROM raw_wellness")
        db._exec("DELETE FROM sync_state WHERE source='garmin_wellness'")
        db._commit()
        last_synced = None
    last_synced = db.get_last_synced("garmin_wellness")
    today = datetime.now().date()

    sync_dates = _compute_sync_dates(last_synced, today, days, unbounded)
    if not sync_dates:
        db.close()
        return {"wellness_records": 0, "with_hrv": 0}

    start_s = min(sync_dates).strftime("%Y-%m-%d")
    end_s = max(sync_dates).strftime("%Y-%m-%d")
    logger.info(f"[personal] Syncing wellness for {len(sync_dates)} days: {start_s} to {end_s}")

    api = PersonalApi()
    reset_rate_limiter()

    # --- Bulk: weight + daily summaries (whole range, no 7-day cap) ---
    weight_by_date: dict[str, float] = {}
    steps_by_date: dict[str, int] = {}
    summaries_by_date: dict[str, dict] = {}

    try:
        rate_limiter().wait()
        weight_by_date = extract_weight(_retry_on_rate_limit(lambda: api.weight_range(start_s, end_s)))
    except GarminPersonalRateLimited:
        rate_limiter().record_429()
        logger.warning("[personal] Rate limited during weight fetch")
        db.close()
        return {"wellness_records": 0, "with_hrv": 0}
    except Exception as e:
        logger.warning(f"[personal] Failed to fetch weight: {e}")

    try:
        rate_limiter().wait()
        summaries_by_date = _retry_on_rate_limit(lambda: api.daily_summaries(start_s, end_s))
        for d, s in summaries_by_date.items():
            st = extract_steps(s)
            if st:
                steps_by_date[d] = st
    except GarminPersonalRateLimited:
        rate_limiter().record_429()
        logger.warning("[personal] Rate limited during daily-summaries fetch")
        db.close()
        return {"wellness_records": 0, "with_hrv": 0}
    except Exception as e:
        logger.warning(f"[personal] Failed to fetch daily summaries: {e}")

    if progress_callback is not None:
        progress_callback(10, "Bulk data fetched, now fetching per-day HRV/sleep...")

    # --- Narrow to days with watch data not already in DB ---
    bulk_dates = set(weight_by_date) | set(steps_by_date)
    existing_dates = db.get_wellness_dates(oldest=start_s, newest=end_s)
    fetch_dates = [
        d for d in sync_dates
        if d.strftime("%Y-%m-%d") in bulk_dates
        and d.strftime("%Y-%m-%d") not in existing_dates
    ]
    logger.info(f"[personal] Fetching {len(fetch_dates)} days with watch data")

    total_stored = 0
    total_with_hrv = 0
    for i, d in enumerate(fetch_dates):
        target = d.strftime("%Y-%m-%d")
        summary = summaries_by_date.get(target)

        hrv = sleep = None
        bb: list = []
        try:
            rate_limiter().wait()
            hrv = _retry_on_rate_limit(lambda: api.hrv(target))
            rate_limiter().wait()
            sleep = _retry_on_rate_limit(lambda: api.sleep(target))
            rate_limiter().wait()
            bb = _retry_on_rate_limit(lambda: api.body_battery(target))
        except GarminPersonalRateLimited:
            rate_limiter().record_429()
            logger.warning(f"[personal] Rate limited during per-day sync for {target}")
            break  # do NOT advance last_synced — retry next run
        except Exception as e:
            logger.debug(f"[personal] Per-day fetch issue for {target}: {e}")

        day = extract_wellness_day(summary, hrv, sleep, bb)
        weight = weight_by_date.get(target)
        steps = steps_by_date.get(target)

        if not any([day["resting_hr"], day["rmssd"], day["stress"], steps, weight,
                    day["spo2"], day["respiration_rate"], day["hydration_ml"],
                    day["intensity_minutes"], day["body_battery"], day["floors"],
                    day["sleep_score"], day["calories"]]):
            logger.info(f"[personal] No wellness data for {target}")
            db.set_last_synced("garmin_wellness", target)
            if progress_callback is not None:
                pct = 10 + int(i / max(len(fetch_dates), 1) * 80)
                progress_callback(min(pct, 95), f"Wellness: {target} ({i+1}/{len(fetch_dates)})")
            time.sleep(GARMIN_WELLNESS_POLL_INTERVAL)
            continue

        try:
            if summary:
                db.store_raw_wellness(target, "daily_summary", summary)
            if hrv:
                db.store_raw_wellness(target, "hrv", hrv)
            if sleep:
                db.store_raw_wellness(target, "sleep", sleep)
            if bb:
                db.store_raw_wellness(target, "body_battery", bb)
        except Exception as e:
            logger.debug(f"[personal] Failed to store raw wellness for {target}: {e}")

        record = {"date": target, "weight": weight, "steps": steps, **day}
        stored = db.store_wellness([record])
        db.set_last_synced("garmin_wellness", target)
        total_stored += stored
        total_with_hrv += 1 if day["rmssd"] is not None else 0
        logger.info(f"[personal] Synced {target}: {stored} record(s)")

        if progress_callback is not None:
            pct = 10 + int(i / max(len(fetch_dates), 1) * 80)
            progress_callback(min(pct, 95), f"Wellness: {target} ({i+1}/{len(fetch_dates)})")
        time.sleep(GARMIN_WELLNESS_POLL_INTERVAL)

    db.close()
    return {"wellness_records": total_stored, "with_hrv": total_with_hrv}


# ── activity sync (Personal API, via the shared pipeline) ─────────────

def sync_activities(
    days: int = 1,
    db_path: str | None = None,
    unbounded: bool = False,
    progress_callback: Callable[[int, str], None] | None = None,
) -> dict[str, int]:
    """Sync activity streams from the Personal API.

    Delegates to the existing ``_sync_activities_batch`` pipeline (offset
    pagination, resume offset, FIT download + parse, refresh) via a
    ``PersonalActivityClient`` adapter, so the behavior matches the
    shared pipeline.
    """
    if db_path is None:
        db_path = str(_config_mod.db_path("cycling_agent.sqlite"))
    if not get_personal_api_key():
        raise RuntimeError(
            "GARMIN_PERSONAL_API_KEY not set. Create a Personal API key in the "
            "Garmin Connect web UI and add it to config.env."
        )

    db = CyclingDB(db_path)
    last_synced = db.get_last_synced("garmin_activities")
    today = datetime.now().date()
    if last_synced:
        try:
            last_date = datetime.strptime(last_synced, "%Y-%m-%d").date()
        except ValueError:
            last_date = today - timedelta(days=1)
    else:
        last_date = today - timedelta(days=1)

    reset_rate_limiter()
    api = PersonalApi()
    client = PersonalActivityClient(api)
    last_synced_date = last_date if not unbounded else None

    total_processed, total_stored = _sync_activities_batch(
        client, db, last_synced_date, unbounded, progress_callback,
    )

    try:
        if total_processed > 0:
            newest = db._exec("SELECT MAX(start_date) FROM activities").fetchone()[0]
            sync_date = newest[:10] if newest else today.isoformat()
            db.set_last_synced("garmin_activities", sync_date, resume_offset=0)
    except Exception:
        logger.warning("[personal] Failed to update last synced date", exc_info=True)

    try:
        db.close()
    except Exception:
        logger.debug("Failed to close database connection", exc_info=True)

    if progress_callback is not None:
        progress_callback(100, f"Sync complete: {total_processed} activities, {total_stored} records")

    return {"activities_processed": total_processed, "stream_records": total_stored}
