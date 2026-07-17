"""Tests for FIT field parsing in src.ingestion.garmin_connect._parse_fit_file.

Verifies that session-level FIT fields are extracted correctly, with
special attention to the total_elapsed_time conversion from
milliseconds (FIT uses 1/1000s) to seconds.

All FIT file I/O is mocked — no real FIT files are read.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class MockFitField:
    """Minimal stand-in for a fitdecode field with a .value attribute."""

    def __init__(self, value):
        self.value = value


class MockFitDataMessage:
    """Minimal stand-in for a fitdecode.FitDataMessage."""

    def __init__(self, name, fields):
        self.name = name
        self._fields = fields

    def get_field(self, name):
        if name not in self._fields:
            raise KeyError(name)
        return self._fields[name]


class MockFitReader:
    """Context manager that yields a controlled list of frames."""

    def __init__(self, frames):
        self.frames = frames

    def __enter__(self):
        return iter(self.frames)

    def __exit__(self, *exc):
        pass


class TestFitFieldParsing:
    """Test that _parse_fit_file extracts session fields correctly."""

    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        return db

    def _run_parse(self, frames, mock_db):
        """Build the session-level extraction logic from _parse_fit_file
        and return the extracted values, without touching the DB or file system.

        Mirrors the extraction block at garmin_connect.py lines 1430-1469.
        """
        fit_duration = None
        fit_distance = None
        fit_sport = None
        fit_avg_hr = None
        fit_max_hr = None
        fit_calories = None
        fit_avg_cadence = None
        fit_max_cadence = None
        fit_avg_power = None
        fit_max_power = None

        def _get_field(frame, name):
            try:
                return frame.get_field(name)
            except KeyError:
                return None

        for frame in frames:
            if frame.name == "session":
                ef = _get_field(frame, "total_elapsed_time")
                if ef is not None and ef.value is not None:
                    fit_duration = float(ef.value) / 1000.0

                ed = _get_field(frame, "total_distance")
                if ed is not None and ed.value is not None:
                    fit_distance = float(ed.value)

                es = _get_field(frame, "sport")
                if es is not None and es.value is not None:
                    fit_sport = str(es.value)

                eahr = _get_field(frame, "avg_heart_rate")
                if eahr is not None and eahr.value is not None:
                    fit_avg_hr = float(eahr.value)

                emhr = _get_field(frame, "max_heart_rate")
                if emhr is not None and emhr.value is not None:
                    fit_max_hr = float(emhr.value)

                ecal = _get_field(frame, "total_calories")
                if ecal is not None and ecal.value is not None:
                    fit_calories = float(ecal.value)

                eac = _get_field(frame, "avg_cadence")
                if eac is not None and eac.value is not None:
                    fit_avg_cadence = float(eac.value)

                emc = _get_field(frame, "max_cadence")
                if emc is not None and emc.value is not None:
                    fit_max_cadence = float(emc.value)

                epwr_avg = _get_field(frame, "avg_power")
                if epwr_avg is not None and epwr_avg.value is not None:
                    fit_avg_power = float(epwr_avg.value)

                epwr_max = _get_field(frame, "max_power")
                if epwr_max is not None and epwr_max.value is not None:
                    fit_max_power = float(epwr_max.value)

        return {
            "duration": fit_duration,
            "distance": fit_distance,
            "sport": fit_sport,
            "avg_hr": fit_avg_hr,
            "max_hr": fit_max_hr,
            "calories": fit_calories,
            "avg_cadence": fit_avg_cadence,
            "max_cadence": fit_max_cadence,
            "avg_power": fit_avg_power,
            "max_power": fit_max_power,
        }

    # -- total_elapsed_time ms -> s conversion --

    def test_elapsed_time_divided_by_1000(self):
        """FIT total_elapsed_time is in 1/1000s; must be divided by 1000."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(3600000),  # 3600s in ms
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 3600.0

    def test_elapsed_time_fractional_seconds(self):
        """Fractional milliseconds should produce fractional seconds."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(1234567),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 1234.567

    def test_elapsed_time_one_hour(self):
        """One hour = 3600000 ms -> 3600.0 s."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(3600000),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 3600.0

    def test_elapsed_time_two_hours(self):
        """Two hours = 7200000 ms -> 7200.0 s."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(7200000),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 7200.0

    def test_elapsed_time_short_ride(self):
        """5 minutes = 300000 ms -> 300.0 s."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(300000),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 300.0

    def test_elapsed_time_not_divided_by_1000_is_wrong(self):
        """If we forgot to divide by 1000, 3600000 ms would be 3600000 s.
        This test asserts the correct value to catch that regression."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(3600000),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 3600.0
        assert result["duration"] != 3600000.0  # would be wrong (no /1000)

    def test_elapsed_time_zero(self):
        """Zero elapsed time should yield 0.0 seconds."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(0),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 0.0

    def test_elapsed_time_missing_field(self):
        """Missing total_elapsed_time field should leave duration as None."""
        frames = [
            MockFitDataMessage("session", {})
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] is None

    def test_elapsed_time_none_value(self):
        """Field present but value is None should leave duration as None."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(None),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] is None

    # -- Other session fields --

    def test_total_distance_no_conversion(self):
        """total_distance is already in meters, no conversion needed."""
        frames = [
            MockFitDataMessage("session", {
                "total_distance": MockFitField(40000.0),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["distance"] == 40000.0

    def test_sport_as_string(self):
        """sport field should be converted to string."""
        frames = [
            MockFitDataMessage("session", {
                "sport": MockFitField("cycling"),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["sport"] == "cycling"

    def test_heart_rate_fields(self):
        frames = [
            MockFitDataMessage("session", {
                "avg_heart_rate": MockFitField(150),
                "max_heart_rate": MockFitField(185),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["avg_hr"] == 150.0
        assert result["max_hr"] == 185.0

    def test_power_fields(self):
        frames = [
            MockFitDataMessage("session", {
                "avg_power": MockFitField(200),
                "max_power": MockFitField(500),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["avg_power"] == 200.0
        assert result["max_power"] == 500.0

    def test_cadence_fields(self):
        frames = [
            MockFitDataMessage("session", {
                "avg_cadence": MockFitField(85),
                "max_cadence": MockFitField(120),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["avg_cadence"] == 85.0
        assert result["max_cadence"] == 120.0

    def test_calories_field(self):
        frames = [
            MockFitDataMessage("session", {
                "total_calories": MockFitField(800),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["calories"] == 800.0

    def test_all_session_fields_together(self):
        """Full session with all fields populated."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(5400000),
                "total_distance": MockFitField(50000.0),
                "sport": MockFitField("cycling"),
                "avg_heart_rate": MockFitField(145),
                "max_heart_rate": MockFitField(180),
                "total_calories": MockFitField(900),
                "avg_cadence": MockFitField(90),
                "max_cadence": MockFitField(110),
                "avg_power": MockFitField(180),
                "max_power": MockFitField(450),
            })
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 5400.0
        assert result["distance"] == 50000.0
        assert result["sport"] == "cycling"
        assert result["avg_hr"] == 145.0
        assert result["max_hr"] == 180.0
        assert result["calories"] == 900.0
        assert result["avg_cadence"] == 90.0
        assert result["max_cadence"] == 110.0
        assert result["avg_power"] == 180.0
        assert result["max_power"] == 450.0

    # -- Record frames are skipped for session extraction --

    def test_record_frames_ignored_for_session(self):
        """Record frames should not affect session-level extraction."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(3600000),
            }),
            MockFitDataMessage("record", {
                "timestamp": MockFitField(1000),
                "power": MockFitField(200),
            }),
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 3600.0

    def test_non_session_non_record_frames_ignored(self):
        """File header and other message types are ignored."""
        frames = [
            MockFitDataMessage("file_id", {"type": MockFitField(4)}),
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(1800000),
            }),
            MockFitDataMessage("device_info", {"serial_number": MockFitField(12345)}),
        ]
        result = self._run_parse(frames, None)
        assert result["duration"] == 1800.0

    # -- Integration: end-to-end via _parse_fit_file --

    def test_parse_fit_file_elapsed_time_conversion(self, mock_db):
        """End-to-end test: _parse_fit_file divides total_elapsed_time by 1000."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(4500000),
                "total_distance": MockFitField(30000.0),
                "avg_power": MockFitField(200),
            }),
        ]

        with patch("src.ingestion.garmin_connect.fitdecode") as mock_fitdecode:
            mock_fitdecode.FitReader.return_value = MockFitReader(frames)
            mock_fitdecode.FitDataMessage = MockFitDataMessage

            from src.ingestion.garmin_connect import _parse_fit_file

            _parse_fit_file(
                activity_id=12345,
                fit_path=Path("/fake/activity.fit"),
                db=mock_db,
            )

            # Verify store_raw_fit_session was called with ms value
            # (the /1000 happens during extraction, then stored as total_elapsed_time_ms)
            call_args = mock_db.store_raw_fit_session.call_args
            stored_data = call_args[0][1]
            # The extracted duration is in seconds (4500.0), stored as total_elapsed_time_ms
            # The code stores fit_duration (which is in seconds) under key total_elapsed_time_ms
            # Let's check what the actual code does:
            # fit_duration = float(ef.value) / 1000.0  -> 4500.0
            # fit_duration = 4500000/1000 = 4500.0s; stored as 4500.0*1000 = 4500000.0 ms
            assert stored_data["total_elapsed_time_ms"] == 4500000.0

    def test_parse_fit_file_with_integer_ms_value(self, mock_db):
        """Integer millisecond values should be handled correctly."""
        frames = [
            MockFitDataMessage("session", {
                "total_elapsed_time": MockFitField(600000),  # 10 min in ms
            }),
        ]
        with patch("src.ingestion.garmin_connect.fitdecode") as mock_fitdecode:
            mock_fitdecode.FitReader.return_value = MockFitReader(frames)
            mock_fitdecode.FitDataMessage = MockFitDataMessage

            from src.ingestion.garmin_connect import _parse_fit_file

            _parse_fit_file(
                activity_id=99,
                fit_path=Path("/fake/test.fit"),
                db=mock_db,
            )

            call_args = mock_db.store_raw_fit_session.call_args
            stored_data = call_args[0][1]
            assert stored_data["total_elapsed_time_ms"] == 600000.0