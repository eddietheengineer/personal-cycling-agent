"""Tests for src.db.store.CyclingDB.

Exercises the public API surface used by the ingestion pipeline:
store_wellness, get_wellness_dates, store_activities, get_activities,
store_morning_checkin, set_last_synced, get_last_synced.

All tests use a temporary SQLite database via tempfile so nothing
touches the real data directory.
"""

import os
import tempfile

import pytest

from src.db.store import CyclingDB


@pytest.fixture
def db():
    """Provide a CyclingDB backed by a temporary file."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    parent = os.path.dirname(tmp.name) or "."
    os.makedirs(parent, exist_ok=True)
    instance = CyclingDB(tmp.name)
    yield instance, tmp.name
    instance.close()
    os.unlink(tmp.name)


# -- store_wellness --


class TestStoreWellness:
    def test_stores_single_record(self, db):
        instance, _ = db
        recs = [{"date": "2025-01-15", "weight": 72.5, "rmssd": 45.0}]
        count = instance.store_wellness(recs)
        assert count == 1

    def test_stores_multiple_records(self, db):
        instance, _ = db
        recs = [
            {"date": "2025-01-14", "weight": 72.0, "resting_hr": 52},
            {"date": "2025-01-15", "weight": 72.5, "resting_hr": 50},
            {"date": "2025-01-16", "weight": 73.0, "resting_hr": 51},
        ]
        count = instance.store_wellness(recs)
        assert count == 3

    def test_upsert_updates_existing_record(self, db):
        instance, _ = db
        instance.store_wellness(
            [{"date": "2025-01-15", "weight": 72.0, "rmssd": 40.0}]
        )
        instance.store_wellness(
            [{"date": "2025-01-15", "weight": 73.0, "rmssd": 50.0}]
        )
        rows = instance.get_wellness()
        assert len(rows) == 1
        assert rows[0]["weight"] == 73.0
        assert rows[0]["rmssd"] == 50.0

    def test_skips_record_without_date(self, db):
        instance, _ = db
        recs = [
            {"date": "2025-01-15", "weight": 72.0},
            {"weight": 73.0},  # no date — should be skipped
        ]
        count = instance.store_wellness(recs)
        assert count == 1

    def test_stores_all_wellness_fields(self, db):
        instance, _ = db
        recs = [
            {
                "date": "2025-01-15",
                "weight": 72.5,
                "resting_hr": 52,
                "rmssd": 45.0,
                "stress": 30,
                "sleep_score": 85,
                "sleep_hours": 7.5,
                "steps": 8000,
                "spo2": 97.0,
                "body_battery_start": 80,
                "body_battery_end": 40,
                "calories": 2500,
                "active_calories": 500,
                "distance_m": 3000,
                "min_hr": 48,
                "max_hr": 160,
            }
        ]
        instance.store_wellness(recs)
        rows = instance.get_wellness()
        row = rows[0]
        assert row["weight"] == 72.5
        assert row["resting_hr"] == 52
        assert row["rmssd"] == 45.0
        assert row["stress"] == 30
        assert row["sleep_score"] == 85
        assert row["sleep_hours"] == 7.5
        assert row["steps"] == 8000
        assert row["spo2"] == 97.0
        assert row["body_battery_start"] == 80
        assert row["body_battery_end"] == 40
        assert row["calories"] == 2500
        assert row["active_calories"] == 500
        assert row["distance_m"] == 3000
        assert row["min_hr"] == 48
        assert row["max_hr"] == 160


# -- get_wellness_dates --


class TestGetWellnessDates:
    def test_returns_empty_set_when_no_records(self, db):
        instance, _ = db
        assert instance.get_wellness_dates() == set()

    def test_returns_dates_for_stored_records(self, db):
        instance, _ = db
        instance.store_wellness(
            [
                {"date": "2025-01-14", "weight": 72.0},
                {"date": "2025-01-15", "weight": 72.5},
                {"date": "2025-01-16", "weight": 73.0},
            ]
        )
        dates = instance.get_wellness_dates()
        assert dates == {"2025-01-14", "2025-01-15", "2025-01-16"}

    def test_filters_by_oldest(self, db):
        instance, _ = db
        instance.store_wellness(
            [
                {"date": "2025-01-14", "weight": 72.0},
                {"date": "2025-01-15", "weight": 72.5},
                {"date": "2025-01-16", "weight": 73.0},
            ]
        )
        dates = instance.get_wellness_dates(oldest="2025-01-15")
        assert dates == {"2025-01-15", "2025-01-16"}

    def test_filters_by_newest(self, db):
        instance, _ = db
        instance.store_wellness(
            [
                {"date": "2025-01-14", "weight": 72.0},
                {"date": "2025-01-15", "weight": 72.5},
                {"date": "2025-01-16", "weight": 73.0},
            ]
        )
        dates = instance.get_wellness_dates(newest="2025-01-15")
        assert dates == {"2025-01-14", "2025-01-15"}

    def test_filters_by_range(self, db):
        instance, _ = db
        instance.store_wellness(
            [
                {"date": "2025-01-14", "weight": 72.0},
                {"date": "2025-01-15", "weight": 72.5},
                {"date": "2025-01-16", "weight": 73.0},
            ]
        )
        dates = instance.get_wellness_dates(
            oldest="2025-01-14", newest="2025-01-15"
        )
        assert dates == {"2025-01-14", "2025-01-15"}


# -- store_activities / get_activities --


class TestStoreActivities:
    def test_stores_single_activity(self, db):
        instance, _ = db
        recs = [
            {
                "id": "garmin_12345",
                "start_date_local": "2025-01-15 08:00:00",
                "type": "cycling",
                "duration": 3600,
                "distance": 40000,
                "average_power": 200,
                "max_power": 400,
                "average_hr": 150,
                "max_hr": 180,
                "calories": 800,
                "tss": 50,
                "ifr": 1.0,
                "normalized_power": 210,
                "file_type": "fit",
            }
        ]
        count = instance.store_activities(recs)
        assert count == 1

    def test_stores_multiple_activities(self, db):
        instance, _ = db
        recs = [
            {
                "id": "garmin_1",
                "start_date_local": "2025-01-14 08:00:00",
                "type": "cycling",
                "duration": 3600,
            },
            {
                "id": "garmin_2",
                "start_date_local": "2025-01-15 09:00:00",
                "type": "running",
                "duration": 1800,
            },
        ]
        count = instance.store_activities(recs)
        assert count == 2

    def test_upsert_replaces_existing_activity(self, db):
        instance, _ = db
        instance.store_activities(
            [{"id": "garmin_1", "start_date_local": "2025-01-15", "duration": 3600}]
        )
        instance.store_activities(
            [{"id": "garmin_1", "start_date_local": "2025-01-15", "duration": 7200}]
        )
        rows = instance.get_activities()
        assert len(rows) == 1
        assert rows[0]["duration"] == 7200

    def test_skips_record_without_id(self, db):
        instance, _ = db
        recs = [
            {"id": "garmin_1", "start_date_local": "2025-01-15", "duration": 3600},
            {"start_date_local": "2025-01-16", "duration": 1800},  # no id
        ]
        count = instance.store_activities(recs)
        assert count == 1


class TestGetActivities:
    def test_returns_empty_when_no_activities(self, db):
        instance, _ = db
        assert instance.get_activities() == []

    def test_returns_all_activities_ordered_desc(self, db):
        instance, _ = db
        instance.store_activities(
            [
                {
                    "id": "garmin_1",
                    "start_date_local": "2025-01-14",
                    "type": "cycling",
                },
                {
                    "id": "garmin_2",
                    "start_date_local": "2025-01-16",
                    "type": "cycling",
                },
                {
                    "id": "garmin_3",
                    "start_date_local": "2025-01-15",
                    "type": "cycling",
                },
            ]
        )
        rows = instance.get_activities()
        assert len(rows) == 3
        assert rows[0]["start_date"] == "2025-01-16"
        assert rows[1]["start_date"] == "2025-01-15"
        assert rows[2]["start_date"] == "2025-01-14"

    def test_filters_by_oldest(self, db):
        instance, _ = db
        instance.store_activities(
            [
                {"id": "garmin_1", "start_date_local": "2025-01-14", "type": "cycling"},
                {"id": "garmin_2", "start_date_local": "2025-01-16", "type": "cycling"},
            ]
        )
        rows = instance.get_activities(oldest="2025-01-15")
        assert len(rows) == 1
        assert rows[0]["start_date"] == "2025-01-16"

    def test_filters_by_newest(self, db):
        instance, _ = db
        instance.store_activities(
            [
                {"id": "garmin_1", "start_date_local": "2025-01-14", "type": "cycling"},
                {"id": "garmin_2", "start_date_local": "2025-01-16", "type": "cycling"},
            ]
        )
        rows = instance.get_activities(newest="2025-01-15")
        assert len(rows) == 1
        assert rows[0]["start_date"] == "2025-01-14"

    def test_filters_by_activity_type(self, db):
        instance, _ = db
        instance.store_activities(
            [
                {"id": "garmin_1", "start_date_local": "2025-01-14", "type": "cycling"},
                {"id": "garmin_2", "start_date_local": "2025-01-15", "type": "running"},
                {"id": "garmin_3", "start_date_local": "2025-01-16", "type": "cycling"},
            ]
        )
        rows = instance.get_activities(activity_type="cycling")
        assert len(rows) == 2

    def test_filters_by_date_range_and_type(self, db):
        instance, _ = db
        instance.store_activities(
            [
                {"id": "garmin_1", "start_date_local": "2025-01-14", "type": "cycling"},
                {"id": "garmin_2", "start_date_local": "2025-01-15", "type": "running"},
                {"id": "garmin_3", "start_date_local": "2025-01-16", "type": "cycling"},
            ]
        )
        rows = instance.get_activities(
            oldest="2025-01-15", newest="2025-01-16", activity_type="cycling"
        )
        assert len(rows) == 1
        assert rows[0]["id"] == "garmin_3"


# -- store_morning_checkin --


class TestStoreMorningCheckin:
    def test_stores_basic_checkin(self, db):
        instance, _ = db
        data = {
            "date": "2025-01-15",
            "soreness": 3,
            "stress": 4,
            "sleep_quality": 7,
            "mood": 6,
            "energy": 5,
            "motivation": 8,
        }
        instance.store_morning_checkin(data)
        checkin = instance.get_morning_checkin("2025-01-15")
        assert checkin is not None
        assert checkin["date"] == "2025-01-15"
        assert checkin["soreness"] == 3
        assert checkin["stress"] == 4
        assert checkin["sleep_quality"] == 7


    def test_boolean_flags_stored_as_int(self, db):
        instance, _ = db
        data = {
            "date": "2025-01-15",
            "caffeine": True,
            "alcohol": False,
            "late_meals": True,
        }
        instance.store_morning_checkin(data)
        checkin = instance.get_morning_checkin("2025-01-15")
        assert checkin["caffeine"] == 1
        assert checkin["alcohol"] == 0
        assert checkin["late_meals"] == 1


    def test_upsert_replaces_existing_checkin(self, db):
        instance, _ = db
        instance.store_morning_checkin(
            {"date": "2025-01-15", "soreness": 2, "mood": 5}
        )
        instance.store_morning_checkin(
            {"date": "2025-01-15", "soreness": 8, "mood": 3}
        )
        checkin = instance.get_morning_checkin("2025-01-15")
        assert checkin["soreness"] == 8
        assert checkin["mood"] == 3

    def test_get_nonexistent_returns_none(self, db):
        instance, _ = db
        assert instance.get_morning_checkin("2099-12-31") is None


# -- set_last_synced / get_last_synced --


class TestSyncState:
    def test_get_last_synced_none_on_fresh_db(self, db):
        instance, _ = db
        assert instance.get_last_synced("garmin_wellness") is None

    def test_set_and_get_last_synced(self, db):
        instance, _ = db
        instance.set_last_synced("garmin_wellness", "2025-01-15")
        assert instance.get_last_synced("garmin_wellness") == "2025-01-15"

    def test_set_last_synced_with_details(self, db):
        instance, _ = db
        instance.set_last_synced(
            "garmin_wellness", "2025-01-15", details="synced 5 days"
        )
        assert instance.get_last_synced("garmin_wellness") == "2025-01-15"

    def test_different_sources_are_independent(self, db):
        instance, _ = db
        instance.set_last_synced("garmin_wellness", "2025-01-15")
        instance.set_last_synced("garmin_activities", "2025-01-14")
        assert instance.get_last_synced("garmin_wellness") == "2025-01-15"
        assert instance.get_last_synced("garmin_activities") == "2025-01-14"

    def test_set_last_synced_overwrites(self, db):
        instance, _ = db
        instance.set_last_synced("garmin_wellness", "2025-01-14")
        instance.set_last_synced("garmin_wellness", "2025-01-15")
        assert instance.get_last_synced("garmin_wellness") == "2025-01-15"


class TestRefreshActivitiesDistance:
    """Test that refresh_activities handles distance correctly.

    Garmin API returns distance in meters (not cm).
    FIT session total_distance is in meters.
    FIT distance should override API distance when available.
    """

    def _setup_raw_data(self, db_inst):
        """Insert raw_activities and raw_fit_sessions for testing."""
        db_inst.store_raw_activity(12345, {
            "startTimeLocal": "2026-01-01T10:00:00",
            "activityTypeKey": "cycling",
            "duration": 3600000,  # 1 hour in ms
            "distance": 30000,  # 30km in meters (API returns meters!)
            "avgPower": 200,
            "maxPower": 400,
            "avgHeartRate": 140,
            "maxHeartRate": 170,
            "calories": 600,
            "raw_json": "{}",
        })

    def test_api_distance_not_divided_by_100(self, db):
        """API distance is in meters and should NOT be divided by 100."""
        inst, _ = db
        self._setup_raw_data(inst)

        # No FIT data — API distance should be used as-is
        count = inst.refresh_activities()
        assert count == 1

        rows = inst.conn.execute("SELECT distance FROM activities WHERE id = 'garmin_12345'").fetchall()
        assert len(rows) == 1
        # API distance is 30000 meters — should NOT be divided by 100
        assert rows[0]["distance"] == 30000.0

    def test_fit_distance_overrides_api(self, db):
        """FIT distance overrides API distance when available."""
        inst, _ = db
        self._setup_raw_data(inst)

        # FIT says 30500m (slightly different from API 30000m)
        inst.store_raw_fit_session(12345, {
            "total_elapsed_time_ms": 3600000,
            "total_distance_m": 30500.0,
            "sport": "cycling",
            "avg_heart_rate": 141.0,
            "max_heart_rate": 171.0,
            "total_calories": 610,
        })

        count = inst.refresh_activities()
        assert count == 1

        rows = inst.conn.execute("SELECT distance FROM activities WHERE id = 'garmin_12345'").fetchall()
        assert rows[0]["distance"] == 30500.0

    def test_fit_distance_zero_does_not_override(self, db):
        """FIT distance of 0 should not override a valid API distance."""
        inst, _ = db
        self._setup_raw_data(inst)

        inst.store_raw_fit_session(12345, {
            "total_elapsed_time_ms": 3600000,
            "total_distance_m": 0,  # zero distance
            "sport": "cycling",
        })

        count = inst.refresh_activities()
        assert count == 1

        rows = inst.conn.execute("SELECT distance FROM activities WHERE id = 'garmin_12345'").fetchall()
        # API distance (30000m) should be kept since FIT is 0
        assert rows[0]["distance"] == 30000.0

    def test_no_fit_data_uses_api_distance(self, db):
        """Activities without FIT data use API distance correctly."""
        inst, _ = db
        self._setup_raw_data(inst)
        # Don't store any FIT session

        count = inst.refresh_activities()
        assert count == 1

        rows = inst.conn.execute("SELECT distance FROM activities WHERE id = 'garmin_12345'").fetchall()
        assert rows[0]["distance"] == 30000.0

    def test_api_distance_short_ride(self, db):
        """Short ride: API distance in meters is correct."""
        inst, _ = db
        inst.store_raw_activity(99999, {
            "startTimeLocal": "2026-01-01T10:00:00",
            "activityTypeKey": "walking",
            "duration": 600000,  # 10 min
            "distance": 1500,  # 1.5km in meters
            "raw_json": "{}",
        })

        count = inst.refresh_activities()
        assert count == 1

        rows = inst.conn.execute("SELECT distance FROM activities WHERE id = 'garmin_99999'").fetchall()
        assert rows[0]["distance"] == 1500.0


class TestRefreshActivitiesPreservesColumns:
    """refresh_activities must not wipe columns it does not recompute.

    power_meter is set by extract_power_meters, ifr/file_type by
    store_activities. An INSERT OR REPLACE that omits them would NULL them
    on every sync/analyze, silently disabling the power-meter exclusion
    filter and dropping display fields.
    """

    def test_preserves_power_meter_ifr_file_type(self, db):
        inst, _ = db
        inst.store_activities([{
            "id": "garmin_12345", "start_date_local": "2026-01-01", "type": "Cycling",
            "duration": 3600, "distance": 30000, "average_power": 200,
            "max_power": 400, "average_hr": 140, "max_hr": 170,
            "calories": 600, "tss": 90, "ifr": 0.8,
            "normalized_power": 200, "file_type": "fit",
        }])
        inst._exec("UPDATE activities SET power_meter = ? WHERE id = ?",
                   ("Garmin:Edge1040", "garmin_12345"))
        inst._commit()

        inst.store_raw_activity(12345, {
            "startTimeLocal": "2026-01-01T10:00:00",
            "activityTypeKey": "cycling",
            "duration": 3600, "distance": 30000,
            "avgPower": 200, "maxPower": 400,
            "avgHeartRate": 140, "maxHeartRate": 170,
            "calories": 600, "trainingStressScore": 90, "normPower": 200,
        })

        count = inst.refresh_activities()
        assert count == 1

        row = inst.conn.execute(
            "SELECT ifr, file_type, power_meter, distance, average_power "
            "FROM activities WHERE id = 'garmin_12345'"
        ).fetchone()
        assert row["power_meter"] == "Garmin:Edge1040", "power_meter wiped by refresh"
        assert row["ifr"] == 0.8, "ifr wiped by refresh"
        assert row["file_type"] == "fit", "file_type wiped by refresh"
        # Recomputed values must still be correct
        assert row["distance"] == 30000.0
        assert row["average_power"] == 200
