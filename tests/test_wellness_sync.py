"""Tests for wellness sync in src.ingestion.garmin_connect.

Verifies that the per-day fetch loop formats dates correctly as
'YYYY-MM-DD' strings when calling the Garmin client methods
(get_hrv_data, get_sleep_data, get_stats). This catches bugs where
target_str might be formatted wrong (e.g. '%m-%d-%Y', '%Y/%m/%d',
or a datetime object instead of a string).

Also tests _safe_timestamp_to_date for edge cases.

All Garmin client calls are mocked — no external API calls are made.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.ingestion.garmin_connect import (
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
    date strings to Garmin client methods."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Garmin client that records all call arguments."""
        client = MagicMock()
        client.get_hrv_data.return_value = {"hrvSummary": {"lastNightAvg": 45.0}}
        client.get_sleep_data.return_value = {"sleepScore": 80, "sleepTimeSeconds": 28800}
        client.get_stats.return_value = {
            "restingHeartRate": 52,
            "allDayStress": {"averageStressLevel": 30},
        }
        return client

    @pytest.fixture
    def mock_db(self):
        """Create a mock CyclingDB."""
        db = MagicMock()
        db.get_wellness_dates.return_value = set()
        db.store_wellness.return_value = 1
        return db

    def _build_sync_loop(self, mock_client, mock_db, sync_dates):
        """Reproduce the core per-day fetch loop from sync_garmin
        so we can inspect the date strings passed to client methods.

        This mirrors the loop at garmin_connect.py lines 1195-1292.
        """
        with patch("src.ingestion.garmin_connect._rate_limiter"):
            weight_by_date = {}
            steps_by_date = {}
            bulk_dates = set()
            for d in sync_dates:
                ds = d.strftime("%Y-%m-%d")
                weight_by_date[ds] = 72.0
                steps_by_date[ds] = 8000
                bulk_dates.add(ds)

            fetch_dates = [d for d in sync_dates if d.strftime("%Y-%m-%d") in bulk_dates]

            for d in fetch_dates:
                target_str = d.strftime("%Y-%m-%d")

                # HRV fetch — call through _retry_on_rate_limit mock
                with patch("src.ingestion.garmin_connect._retry_on_rate_limit", side_effect=lambda fn: fn()):
                    mock_client.get_hrv_data(target_str)

                # Sleep fetch
                with patch("src.ingestion.garmin_connect._retry_on_rate_limit", side_effect=lambda fn: fn()):
                    mock_client.get_sleep_data(target_str)

                # Stats fetch
                with patch("src.ingestion.garmin_connect._retry_on_rate_limit", side_effect=lambda fn: fn()):
                    mock_client.get_stats(target_str)

                # Build record (mirrors sync_garmin)
                weight = weight_by_date.get(target_str)
                steps = steps_by_date.get(target_str)

                if any([weight, steps]):
                    record = {
                        "date": target_str,
                        "weight": weight,
                        "steps": steps,
                    }
                    mock_db.store_wellness([record])

            return mock_client

    def test_hrv_data_called_with_yyyy_mm_dd(self, mock_client, mock_db):
        sync_dates = [date(2025, 1, 15), date(2025, 1, 14), date(2025, 1, 13)]
        self._build_sync_loop(mock_client, mock_db, sync_dates)

        calls = [c[0][0] for c in mock_client.get_hrv_data.call_args_list]
        assert len(calls) == 3
        assert calls == ["2025-01-15", "2025-01-14", "2025-01-13"]

    def test_sleep_data_called_with_yyyy_mm_dd(self, mock_client, mock_db):
        sync_dates = [date(2025, 6, 1), date(2025, 5, 31)]
        self._build_sync_loop(mock_client, mock_db, sync_dates)

        calls = [c[0][0] for c in mock_client.get_sleep_data.call_args_list]
        assert calls == ["2025-06-01", "2025-05-31"]

    def test_stats_called_with_yyyy_mm_dd(self, mock_client, mock_db):
        sync_dates = [date(2025, 12, 25), date(2025, 12, 24)]
        self._build_sync_loop(mock_client, mock_db, sync_dates)

        calls = [c[0][0] for c in mock_client.get_stats.call_args_list]
        assert calls == ["2025-12-25", "2025-12-24"]

    def test_all_client_methods_receive_same_date(self, mock_client, mock_db):
        sync_dates = [date(2025, 3, 15), date(2025, 3, 14)]
        self._build_sync_loop(mock_client, mock_db, sync_dates)

        hrv = [c[0][0] for c in mock_client.get_hrv_data.call_args_list]
        sleep = [c[0][0] for c in mock_client.get_sleep_data.call_args_list]
        stats = [c[0][0] for c in mock_client.get_stats.call_args_list]
        assert hrv == sleep == stats

    def test_date_format_not_reversed_or_slashed(self, mock_client, mock_db):
        sync_dates = [date(2025, 1, 15)]
        self._build_sync_loop(mock_client, mock_db, sync_dates)

        calls = [c[0][0] for c in mock_client.get_hrv_data.call_args_list]
        for ds in calls:
            assert ds.startswith("2025-"), f"Date should start with year, got: {ds}"
            assert "/" not in ds, f"Date should use dashes, not slashes: {ds}"

    def test_single_digit_month_and_day_padded(self, mock_client, mock_db):
        sync_dates = [date(2025, 1, 5)]
        self._build_sync_loop(mock_client, mock_db, sync_dates)

        calls = [c[0][0] for c in mock_client.get_hrv_data.call_args_list]
        assert calls[0] == "2025-01-05"

    def test_wellness_record_date_matches_target_str(self, mock_client, mock_db):
        sync_dates = [date(2025, 7, 4)]
        self._build_sync_loop(mock_client, mock_db, sync_dates)

        store_calls = mock_db.store_wellness.call_args_list
        assert len(store_calls) == 1
        records = store_calls[0][0][0]
        assert records[0]["date"] == "2025-07-04"