"""
Garmin Connect data export importer.

Processes the Garmin "Download your data" ZIP file and extracts:
- Daily wellness from DI-Connect-Aggregator/UDSFile_*.json (RHR, stress, steps, SpO2, body battery)
- Activities from DI-Connect-Fitness/*_summarizedActivities.json (power, HR, distance, TSS, etc.)

Note: Garmin's data export does NOT include RMSSD/HRV. That data is only available
via the Garmin Connect API (ingested through Intervals.icu). This importer provides
everything else from the raw export as a historical baseline.

Usage:
    from src.ingestion.garmin_export import import_garmin_export
    counts = import_garmin_export("/path/to/garmin_export.zip")
"""

import json
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src import config
from src.db.store import CyclingDB

logger = logging.getLogger(__name__)

# UDSFile pattern: UDSFile_YYYY-MM-DD_YYYY-MM-DD.json
_UDS_PATTERN = re.compile(r"UDSFile_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json")

# Summarized activities pattern: userjet2005_*_summarizedActivities.json
_ACTIVITIES_PATTERN = re.compile(r".*_summarizedActivities\.json$")


def _find_garmin_root(zip_path: str) -> str:
    """Find the root directory name inside the Garmin ZIP."""
    with zipfile.ZipFile(zip_path) as zf:
        # The root is the first directory entry (e.g. "f51c674b-.../")
        for name in zf.namelist():
            if name.endswith("/") and "__MACOSX" not in name and "/" not in name[:-1]:
                return name[:-1]  # strip trailing slash
    raise ValueError(f"Could not find root directory in {zip_path}")


def _parse_uds_files(zip_path: str, root: str) -> list[dict[str, Any]]:
    """Extract daily wellness records from UDSFile_*.json."""
    records = []

    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not _UDS_PATTERN.search(name):
                continue
            if "__MACOSX" in name:
                continue

            logger.info(f"Parsing {name}")
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, zipfile.BadZipFile) as e:
                logger.warning(f"Failed to parse {name}: {e}")
                continue

            if isinstance(data, list):
                for record in data:
                    wellness = _extract_wellness(record)
                    if wellness:
                        records.append(wellness)
            elif isinstance(data, dict):
                wellness = _extract_wellness(data)
                if wellness:
                    records.append(wellness)

    # Deduplicate by date (keep last occurrence)
    by_date: dict[str, dict] = {}
    for r in records:
        by_date[r["date"]] = r
    return list(by_date.values())


def _extract_wellness(record: dict[str, Any]) -> dict[str, Any] | None:
    """Extract wellness fields from a UDS record."""
    date = record.get("calendarDate")
    if not date:
        return None

    return {
        "date": date,
        "weight": None,  # not in UDS
        "resting_hr": record.get("currentDayRestingHeartRate") or record.get("restingHeartRate"),
        "rmssd": None,  # not available in Garmin export
        "stress": _get_stress(record),
        "sleep_score": None,  # not directly available
        "sleep_hours": _get_sleep_hours(record),
        "steps": record.get("totalSteps"),
        "spo2": record.get("averageSpo2Value"),
        "body_battery_start": _get_body_battery(record, "start"),
        "body_battery_end": _get_body_battery(record, "end"),
        "calories": record.get("totalKilocalories"),
        "active_calories": record.get("activeKilocalories"),
        "distance_m": record.get("totalDistanceMeters"),
        "min_hr": record.get("minHeartRate"),
        "max_hr": record.get("maxHeartRate"),
    }


def _get_stress(record: dict[str, Any]) -> float | None:
    """Extract average stress level from allDayStress."""
    stress_data = record.get("allDayStress")
    if not stress_data:
        return None
    agg_list = stress_data.get("aggregatorList", [])
    for agg in agg_list:
        if agg.get("type") == "TOTAL":
            return agg.get("averageStressLevel")
    return None


def _get_sleep_hours(record: dict[str, Any]) -> float | None:
    """Estimate sleep hours from wellness time range if sleep data is present."""
    # Garmin UDS doesn't have direct sleep duration in the aggregator
    # We'd need the sleep-specific JSON files for this
    return None


def _get_body_battery(record: dict[str, Any], point: str = "end") -> int | None:
    """Extract body battery level."""
    bb = record.get("bodyBattery")
    if not bb:
        return None
    if point == "start":
        return bb.get("startOfDisplayPeriodBodyBattery")
    return bb.get("endOfDisplayPeriodBodyBattery")


def _parse_activity_files(zip_path: str, root: str) -> list[dict[str, Any]]:
    """Extract activity summaries from *_summarizedActivities.json."""
    activities = []

    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not _ACTIVITIES_PATTERN.search(name):
                continue
            if "__MACOSX" in name:
                continue

            logger.info(f"Parsing {name}")
            try:
                data = json.loads(zf.read(name))
            except (json.JSONDecodeError, zipfile.BadZipFile) as e:
                logger.warning(f"Failed to parse {name}: {e}")
                continue

            # Garmin wraps activities in [{ "summarizedActivitiesExport": [...] }]
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        act_list = item.get("summarizedActivitiesExport", [])
                        for act in act_list:
                            activity = _extract_activity(act)
                            if activity:
                                activities.append(activity)
            elif isinstance(data, dict):
                act_list = data.get("summarizedActivitiesExport", [])
                for act in act_list:
                    activity = _extract_activity(act)
                    if activity:
                        activities.append(activity)

    return activities


def _extract_activity(record: dict[str, Any]) -> dict[str, Any] | None:
    """Extract activity fields from a summarized activity record."""
    activity_id = record.get("activityId")
    if not activity_id:
        return None

    # Convert Garmin timestamp to ISO local date
    start_local = record.get("startTimeLocal")
    if start_local:
        dt = datetime.fromtimestamp(start_local / 1000)
        start_date = dt.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        start_date = None

    sport = record.get("sportType", "").lower()
    type_map = {
        "cycling": "Ride",
        "running": "Run",
        "walking": "Walk",
        "swimming": "Swim",
        "indoor_cycling": "Indoor Cycle",
        "strength_training": "Strength Training",
    }
    activity_type = type_map.get(sport, sport.title())

    return {
        "id": f"garmin_{activity_id}",
        "start_date_local": start_date,
        "type": activity_type,
        "duration": record.get("duration"),
        "distance": record.get("distance"),
        "average_power": record.get("avgPower"),
        "max_power": record.get("maxPower"),
        "average_hr": record.get("avgHeartRate"),
        "max_hr": record.get("maxHeartRate"),
        "calories": record.get("calories"),
        "tss": record.get("trainingStressScore"),
        "ifr": None,
        "normalized_power": record.get("normPower"),
        "file_type": None,
    }


def import_garmin_export(
    zip_path: str,
    db_path: str | None = None,
) -> dict[str, int]:
    """
    Import a Garmin Connect data export ZIP into the cycling agent database.

    Args:
        zip_path: Path to the Garmin export ZIP file.
        db_path: Optional override for the database path.

    Returns:
        Dict with counts of imported wellness and activity records.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"Garmin export not found: {zip_path}")

    vault = config.vault_path()
    if db_path is None:
        db_path = str(config.db_path("cycling_agent.sqlite"))

    logger.info(f"Importing Garmin export: {zip_path}")

    # Find root directory in ZIP
    root = _find_garmin_root(zip_path)
    logger.info(f"Garmin export root: {root}")

    # Parse wellness data
    wellness_records = _parse_uds_files(zip_path, root)
    logger.info(f"Extracted {len(wellness_records)} wellness records")

    # Parse activity data
    activity_records = _parse_activity_files(zip_path, root)
    logger.info(f"Extracted {len(activity_records)} activity records")

    # Save raw ZIP to vault
    raw_dir = config.raw_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)
    zip_name = os.path.basename(zip_path)
    dest = raw_dir / f"garmin_export_{datetime.now().strftime('%Y%m%d')}_{zip_name}"
    if not dest.exists():
        import shutil
        shutil.copy2(zip_path, dest)
        logger.info(f"Archived raw export to {dest}")

    # Store in database
    db = CyclingDB(db_path)
    stored_wellness = db.store_wellness(wellness_records)
    stored_activities = db.store_activities(activity_records)
    db.close()

    counts = {
        "wellness_records": stored_wellness,
        "activity_records": stored_activities,
    }
    logger.info(f"Garmin import complete: {counts}")
    return counts

try:
    from fitparse import FitFile
except ImportError:
    FitFile = None  # type: ignore


def sync_routes_from_fit(db: CyclingDB, raw_dir: Path | None = None) -> dict[str, int]:
    """Parse FIT files and store GPS route data in the database.

    Scans raw_dir for .fit files, extracts position_lat/position_long from
    record messages, converts from FIT integer format (divide by 1E7), and
    stores the route points via db.store_routes().

    Args:
        db: An open CyclingDB instance.
        raw_dir: Directory containing FIT files. Defaults to config.raw_dir().

    Returns:
        Dict with keys "processed", "with_gps", "without_gps", "total_points".
    """
    if FitFile is None:
        logger.warning("fitparse not installed — cannot parse FIT files")
        return {"processed": 0, "with_gps": 0, "without_gps": 0, "total_points": 0}

    if raw_dir is None:
        raw_dir = config.raw_dir()
    raw_dir = Path(raw_dir)

    if not raw_dir.is_dir():
        raise FileNotFoundError(f"FIT directory not found: {raw_dir}")

    fit_files = sorted(raw_dir.rglob("*.fit"))
    logger.info(f"Found {len(fit_files)} FIT files in {raw_dir}")

    counts = {"processed": 0, "with_gps": 0, "without_gps": 0, "total_points": 0}

    for fit_path in fit_files:
        activity_id = fit_path.stem  # bare numeric filename, e.g. "21634975856"

        # Skip if already has routes
        if db.get_route_count_for_activity(activity_id) > 0:
            counts["processed"] += 1
            continue

        try:
            fit = FitFile(str(fit_path))
        except Exception:
            logger.warning(f"Failed to parse {fit_path.name!r}")
            counts["processed"] += 1
            counts["without_gps"] += 1
            continue

        records = list(fit.get_messages("record"))
        fit.close()

        if not records:
            counts["processed"] += 1
            counts["without_gps"] += 1
            continue

        # Check if the first record has position_lat (indicates GPS data present)
        first = records[0]
        if first.get_value("position_lat") is None:
            counts["processed"] += 1
            counts["without_gps"] += 1
            continue

        # Extract (lat, lon) pairs, converting from FIT integer format
        points: list[tuple[float, float]] = []
        for rec in records:
            lat = rec.get_value("position_lat")
            lon = rec.get_value("position_long")
            if lat is not None and lon is not None:
                points.append((lat / 1e7, lon / 1e7))

        if not points:
            counts["processed"] += 1
            counts["without_gps"] += 1
            continue

        inserted = db.store_routes(activity_id, points)
        counts["processed"] += 1
        counts["with_gps"] += 1
        counts["total_points"] += inserted
        logger.info(
            f"{activity_id}: {len(points)} GPS points "
            f"({inserted} inserted)"
        )

    logger.info(f"Route sync complete: {counts}")
    return counts

if __name__ == "__main__":
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    config.setup()

    if len(_sys.argv) < 2:
        print("Usage: python -m src.ingestion.garmin_export <path_to_garmin_export.zip>")
        raise SystemExit(1)

    counts = import_garmin_export(_sys.argv[1])
    print(f"Done. Wellness: {counts['wellness_records']}, Activities: {counts['activity_records']}")