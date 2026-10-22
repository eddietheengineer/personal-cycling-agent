"""Tests for wellness sync in src.ingestion.garmin_personal.

Verifies that the per-day fetch loop formats dates correctly as
'YYYY-MM-DD' strings when calling the Personal API client methods
(hrv, sleep, body_battery). This catches bugs where the date string
might be formatted wrong (e.g. '%m-%d-%Y', '%Y/%m/%d', or a datetime
object instead of a string).

Also tests _safe_timestamp_to_date for edge cases and the unbounded
forward catch-up date logic.

All Garmin API calls are mocked — no external API calls are made.
"""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.ingestion.garmin_pipeline import (
    _safe_timestamp_to_date,
)


# -- _safe_timestamp_to_date --


class TestSafeTimestampToDate:
    def test_converts_valid_timestamp(self):
        # 2025-01-15 00:00:00 UTC ≈ 1736899200000 ms
        result = _safe_timestamp_to_date(1736899200000)
        assert result is not None
        assert result.startswith("2025-")

    def test_converts_integer_timestamp(self):
        result = _safe_timestamp_to_date(1736899200000)
        assert result is not None

    def test_converts_float_timestamp(self):
        result = _safe_timestamp_to_date(1736899200000.0)
        assert result is not None

    def test_returns_none_for_none(self):
        assert _safe_timestamp_to_date(None) is None

    def test_returns_none_for_zero(self):
        assert _safe_timestamp_to_date(0) is None

    def test_returns_none_for_negative(self):
        assert _safe_timestamp_to_date(-1000) is None

    def test_returns_none_for_garbage_string(self):
        assert _safe_timestamp_to_date("not-a-number") is None

    def test_returns_none_for_extremely_large_value(self):
        # Overflow territory
        assert _safe_timestamp_to_date(1e20) is None

    def test_format_is_yyyy_mm_dd(self):
        # Use a known timestamp: 2025-06-15 00:00:00 UTC = 1750099200000 ms
        result = _safe_timestamp_to_date(1750099200000)
        assert result is not None
        # Verify the format has dashes as separators
        parts = result.split("-")
        assert len(parts) == 3
        assert len(parts[0]) == 4  # year
        assert len(parts[1]) == 2  # month
        assert len(parts[2]) == 2  # day


# -- Wellness sync date formatting --


class TestWellnessSyncDateFormatting:
    """Verify that the wellness sync loop passes correctly formatted
    date strings to the Personal API client methods.

    Exercises the real ``sync_garmin`` with a mocked ``PersonalApi`` and
    ``CyclingDB`` — no network, no real DB.
    """

    @pytest.fixture
    def mock_api(self):
        """Create a mock PersonalApi that records all call arguments.

        daily_summaries returns a step value for every date in the requested
        range so the per-day fetch loop considers those dates (it only
        fetches days that have bulk/watch data).
        """
        api = MagicMock()
        api.weight_range.return_value = {}

        def _summaries(start, end):
            out = {}
            d = datetime.strptime(start, "%Y-%m-%d").date()
            end_d = datetime.strptime(end, "%Y-%m-%d").date()
            while d <= end_d:
                out[d.strftime("%Y-%m-%d")] = {"movement": {"steps": {"value": 8000}}}
                d += timedelta(days=1)
            return out

        api.daily_summaries.side_effect = _summaries
        api.hrv.return_value = {"hrvSummary": {"lastNightAvg": 45.0}}
        api.sleep.return_value = {"sleepScore": 80, "sleepTimeSeconds": 28800}
        api.body_battery.return_value = []
        return api

    @pytest.fixture
    def mock_db(self):
        """Create a mock CyclingDB."""
        db = MagicMock()
        db.get_last_synced.return_value = "2025-01-12"
        db.get_wellness_dates.return_value = set()
        db.store_wellness.return_value = 1
        return db

    def _run_sync(self, mock_api, mock_db, days):
        """Run the real sync_garmin with everything mocked."""
        from src.ingestion import garmin_personal as gp

        with patch.object(gp, "PersonalApi", return_value=mock_api), \
             patch.object(gp, "CyclingDB", return_value=mock_db), \
             patch.object(gp, "get_personal_api_key", return_value="test-key"), \
             patch.object(gp, "rate_limiter"), \
             patch.object(gp, "reset_rate_limiter"), \
             patch.object(gp, "GARMIN_WELLNESS_POLL_INTERVAL", 0):
            return gp.sync_garmin(days=days, db_path="/tmp/test_dates.db")

    def test_hrv_called_with_yyyy_mm_dd(self, mock_api, mock_db):
        self._run_sync(mock_api, mock_db, days=3)

        calls = [c[0][0] for c in mock_api.hrv.call_args_list]
        assert len(calls) == 3
        assert calls == ["2025-01-13", "2025-01-14", "2025-01-15"]

    def test_sleep_called_with_yyyy_mm_dd(self, mock_api, mock_db):
        self._run_sync(mock_api, mock_db, days=2)

        calls = [c[0][0] for c in mock_api.sleep.call_args_list]
        assert calls == ["2025-01-13", "2025-01-14"]

    def test_body_battery_called_with_yyyy_mm_dd(self, mock_api, mock_db):
        self._run_sync(mock_api, mock_db, days=2)

        calls = [c[0][0] for c in mock_api.body_battery.call_args_list]
        assert calls == ["2025-01-13", "2025-01-14"]

    def test_all_client_methods_receive_same_date(self, mock_api, mock_db):
        self._run_sync(mock_api, mock_db, days=2)

        hrv = [c[0][0] for c in mock_api.hrv.call_args_list]
        sleep = [c[0][0] for c in mock_api.sleep.call_args_list]
        bb = [c[0][0] for c in mock_api.body_battery.call_args_list]
        assert hrv == sleep == bb

    def test_date_format_not_reversed_or_slashed(self, mock_api, mock_db):
        self._run_sync(mock_api, mock_db, days=1)

        calls = [c[0][0] for c in mock_api.hrv.call_args_list]
        for ds in calls:
            assert ds.startswith("2025-"), f"Date should start with year, got: {ds}"
            assert "/" not in ds, f"Date should use dashes, not slashes: {ds}"

    def test_single_digit_month_and_day_padded(self, mock_api, mock_db):
        mock_db.get_last_synced.return_value = "2025-01-04"
        self._run_sync(mock_api, mock_db, days=1)

        calls = [c[0][0] for c in mock_api.hrv.call_args_list]
        assert calls[0] == "2025-01-05"

    def test_wellness_record_date_matches_target(self, mock_api, mock_db):
        self._run_sync(mock_api, mock_db, days=1)

        store_calls = mock_db.store_wellness.call_args_list
        assert len(store_calls) == 1
        records = store_calls[0][0][0]
        assert records[0]["date"] == "2025-01-13"


# -- Unbounded forward catch-up (regression) --


class TestUnboundedForwardCatchUp:
    """Regression: unbounded sync must catch up FORWARD from last_synced to
    today. It previously went backwards from last_synced, which can never
    close a forward gap — it re-synced already-stored days instead.

    All Personal API calls and the DB are mocked — no network, no real DB.
    """

    def _run_unbounded(self, last_synced, today):
        import datetime as real_datetime
        from src.ingestion import garmin_personal as gp

        class FakeDateTime(real_datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(today.year, today.month, today.day, 12, 0, 0)

        mock_db = MagicMock()
        mock_db.get_last_synced.return_value = last_synced
        mock_db.get_wellness_dates.return_value = set()

        mock_api = MagicMock()
        # Bulk fetches return empty so no per-day fetches are triggered.
        mock_api.weight_range.return_value = {}
        mock_api.daily_summaries.return_value = {}

        with patch.object(gp, "datetime", FakeDateTime), \
             patch.object(gp, "PersonalApi", return_value=mock_api), \
             patch.object(gp, "CyclingDB", return_value=mock_db), \
             patch.object(gp, "get_personal_api_key", return_value="test-key"), \
             patch.object(gp, "rate_limiter"), \
             patch.object(gp, "reset_rate_limiter"):
            gp.sync_garmin(unbounded=True, db_path="/tmp/test_unbounded.db")
        return mock_api

    def test_forward_catch_up_from_past_last_synced(self):
        today = date(2026, 10, 10)
        api = self._run_unbounded("2026-09-01", today)
        args = api.weight_range.call_args[0]
        assert args[0] == "2026-09-02"  # last_synced + 1 day
        assert args[1] == "2026-10-10"  # today

    def test_already_up_to_date_makes_no_client_calls(self):
        today = date(2026, 10, 10)
        api = self._run_unbounded("2026-10-10", today)  # last_synced == today
        api.weight_range.assert_not_called()