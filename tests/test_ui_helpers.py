"""Unit tests for src/ui_helpers.py"""

import pytest

from src.ui_helpers import (
    _format_duration,
    _distance_km,
    _stream_id,
    _downsample,
    _elapsed_to_minutes,
    _zone_for_value,
    _make_zones,
    _parse_profile_text,
    _ZONE_RANGES,
    _HR_RANGES,
    _LIGHT_COLORS,
    _DARK_COLORS,
)


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_hours_minutes_seconds(self):
        assert _format_duration(3661000) == "1h 1m 1s"

    def test_minutes_seconds(self):
        assert _format_duration(90000) == "1m 30s"

    def test_none(self):
        assert _format_duration(None) == "\u2014"

    def test_zero(self):
        assert _format_duration(0) == "0m 0s"

    def test_only_seconds(self):
        assert _format_duration(45000) == "0m 45s"

    def test_exactly_one_hour(self):
        assert _format_duration(3600000) == "1h 0m 0s"

    def test_large_duration(self):
        # 5h 30m 15s = 19815 seconds = 19815000 ms
        assert _format_duration(19815000) == "5h 30m 15s"

    def test_sub_second_truncation(self):
        # 900.5 seconds -> int(900.5) = 900 -> 15m 0s
        assert _format_duration(900500) == "15m 0s"


# ---------------------------------------------------------------------------
# _distance_km
# ---------------------------------------------------------------------------


class TestDistanceKm:
    def test_normal(self):
        assert _distance_km(5000000) == "50.00 km"

    def test_zero_returns_dash(self):
        assert _distance_km(0) == "\u2014"

    def test_none_returns_dash(self):
        assert _distance_km(None) == "\u2014"

    def test_precision(self):
        assert _distance_km(1234567) == "12.35 km"

    def test_one_km(self):
        assert _distance_km(100000) == "1.00 km"

    def test_fractional_km(self):
        assert _distance_km(12345) == "0.12 km"


# ---------------------------------------------------------------------------
# _stream_id
# ---------------------------------------------------------------------------


class TestStreamId:
    def test_strips_prefix(self):
        assert _stream_id("garmin_12345") == "12345"

    def test_no_prefix(self):
        assert _stream_id("12345") == "12345"

    def test_empty_string(self):
        assert _stream_id("") == ""

    def test_prefix_only(self):
        assert _stream_id("garmin_") == ""

    def test_no_false_match(self):
        assert _stream_id("garminlike_99") == "garminlike_99"


# ---------------------------------------------------------------------------
# _downsample
# ---------------------------------------------------------------------------


class TestDownsample:
    def test_no_op_under_limit(self):
        elapsed = list(range(50))
        values = list(range(50, 100))
        e_out, v_out = _downsample(elapsed, values, max_points=100)
        assert e_out == elapsed
        assert v_out == values

    def test_no_op_at_limit(self):
        n = 100
        elapsed = list(range(n))
        values = list(range(n, n * 2))
        e_out, v_out = _downsample(elapsed, values, max_points=n)
        assert e_out == elapsed
        assert v_out == values

    def test_reduces_over_limit(self):
        n = 200
        elapsed = list(range(n))
        values = list(range(n, n * 2))
        e_out, v_out = _downsample(elapsed, values, max_points=50)
        assert len(e_out) == 50
        assert len(v_out) == 50

    def test_preserves_order(self):
        n = 500
        elapsed = list(range(n))
        values = [i * 2 for i in range(n)]
        e_out, v_out = _downsample(elapsed, values, max_points=10)
        for i in range(1, len(e_out)):
            assert e_out[i] > e_out[i - 1]
        for i in range(1, len(v_out)):
            assert v_out[i] > v_out[i - 1]

    def test_preserves_first(self):
        n = 1000
        elapsed = list(range(n))
        values = list(range(n))
        e_out, v_out = _downsample(elapsed, values, max_points=5)
        assert e_out[0] == 0
        assert v_out[0] == 0

    def test_returns_lists_not_arrays(self):
        elapsed = list(range(200))
        values = list(range(200))
        e_out, v_out = _downsample(elapsed, values, max_points=10)
        assert isinstance(e_out, list)
        assert isinstance(v_out, list)

    def test_empty_input(self):
        e_out, v_out = _downsample([], [], max_points=10)
        assert e_out == []
        assert v_out == []

    def test_zero_max_points(self):
        elapsed = list(range(100))
        values = list(range(100))
        e_out, v_out = _downsample(elapsed, values, max_points=0)
        assert e_out == elapsed
        assert v_out == values

    def test_mismatched_lengths_raises(self):
        with pytest.raises(ValueError, match="same length"):
            _downsample([1, 2, 3], [1, 2], max_points=10)


# ---------------------------------------------------------------------------
# _zone_for_value
# ---------------------------------------------------------------------------


class TestZoneForValue:
    def test_zone_1(self):
        # 50% of threshold -> within Z1 (0.0 to 0.55)
        assert _zone_for_value(55, 100, _ZONE_RANGES) == 0

    def test_zone_3(self):
        # 85% of threshold -> within Z3 (0.76 to 0.90)
        assert _zone_for_value(85, 100, _ZONE_RANGES) == 2

    def test_zone_5(self):
        # 110% of threshold -> within Z5 (1.05 to 999)
        assert _zone_for_value(110, 100, _ZONE_RANGES) == 4

    def test_zero_threshold(self):
        assert _zone_for_value(50, 0, _ZONE_RANGES) == -1

    def test_negative_threshold(self):
        assert _zone_for_value(50, -10, _ZONE_RANGES) == -1

    def test_boundary_values(self):
        # Exact lower boundary of Z2 (0.55)
        assert _zone_for_value(55, 100, _ZONE_RANGES) == 0
        # Exact upper boundary of Z2 (0.75)
        assert _zone_for_value(75, 100, _ZONE_RANGES) == 1
        # Exact lower boundary of Z3 (0.76)
        assert _zone_for_value(76, 100, _ZONE_RANGES) == 2
        # Exact upper boundary of Z4 (1.05)
        assert _zone_for_value(105, 100, _ZONE_RANGES) == 3

    def test_gap_between_zones(self):
        # 0.755 is between Z2 hi=0.75 and Z3 lo=0.76 -> no match -> -1
        assert _zone_for_value(75.5, 100, _ZONE_RANGES) == -1

    def test_hr_zones(self):
        # 70% of max HR -> Z2 (0.59 to 0.74)
        assert _zone_for_value(70, 100, _HR_RANGES) == 1

    def test_value_below_all_zones(self):
        # ratio = 0, which is >= 0.0 (Z1 lo) and <= 0.55 (Z1 hi)
        assert _zone_for_value(0, 100, _ZONE_RANGES) == 0


# ---------------------------------------------------------------------------
# _make_zones
# ---------------------------------------------------------------------------


class TestMakeZones:
    def test_combines_ranges_and_colors(self):
        zones = _make_zones(_ZONE_RANGES, _LIGHT_COLORS)
        assert len(zones) == 5
        assert len(zones[0]) == 4
        assert zones[0][0] == 0.0
        assert zones[0][1] == 0.55
        assert zones[0][2] == "Z1: Active Recovery"
        assert zones[0][3] == "#1f77b4"

    def test_hr_zones_with_dark_colors(self):
        zones = _make_zones(_HR_RANGES, _DARK_COLORS)
        assert zones[4][2] == "Z5: VO2/Neuromuscular"
        assert zones[4][3] == "#c99fff"

    def test_mismatched_lengths(self):
        # zip truncates to shortest
        zones = _make_zones(_ZONE_RANGES, ["#ff0000"])
        assert len(zones) == 1

    def test_empty(self):
        zones = _make_zones([], [])
        assert zones == []


# ---------------------------------------------------------------------------
# _elapsed_to_minutes
# ---------------------------------------------------------------------------


class TestElapsedToMinutes:
    def test_basic(self):
        assert _elapsed_to_minutes(3600) == 60.0

    def test_zero(self):
        assert _elapsed_to_minutes(0) == 0.0

    def test_fractional(self):
        assert _elapsed_to_minutes(90) == 1.5

    def test_negative(self):
        assert _elapsed_to_minutes(-60) == -1.0


# ---------------------------------------------------------------------------
# _parse_profile_text
# ---------------------------------------------------------------------------


class TestParseProfileText:
    def test_parses_string_field(self):
        raw = "- Name: John Doe\n"
        profile = {"name": ""}
        result = _parse_profile_text(raw, profile)
        assert result["name"] == "John Doe"

    def test_parses_int_field(self):
        raw = "- FTP Watts: 280\n"
        profile = {"ftp_watts": 0}
        result = _parse_profile_text(raw, profile)
        assert result["ftp_watts"] == 280

    def test_parses_weight(self):
        raw = "- Weight (kg): 75\n"
        profile = {"weight_kg": 0}
        result = _parse_profile_text(raw, profile)
        assert result["weight_kg"] == 75

    def test_parses_height(self):
        raw = "- Height (cm): 180\n"
        profile = {"height_cm": 0}
        result = _parse_profile_text(raw, profile)
        assert result["height_cm"] == 180

    def test_parses_max_hr(self):
        raw = "- Max HR: 190\n"
        profile = {"max_hr": 0}
        result = _parse_profile_text(raw, profile)
        assert result["max_hr"] == 190

    def test_parses_resting_hr(self):
        raw = "- Resting HR (avg): 45\n"
        profile = {"resting_hr": 0}
        result = _parse_profile_text(raw, profile)
        assert result["resting_hr"] == 45

    def test_parses_discipline(self):
        raw = "- Primary Discipline: Road\n"
        profile = {"discipline": ""}
        result = _parse_profile_text(raw, profile)
        assert result["discipline"] == "Road"

    def test_parses_multiple_fields(self):
        raw = "- Name: Alice\n- FTP Watts: 250\n- Weight (kg): 65\n"
        profile = {"name": "", "ftp_watts": 0, "weight_kg": 0}
        result = _parse_profile_text(raw, profile)
        assert result["name"] == "Alice"
        assert result["ftp_watts"] == 250
        assert result["weight_kg"] == 65

    def test_ignores_placeholder_int(self):
        raw = "- FTP Watts: [Insert your FTP]\n"
        profile = {"ftp_watts": 0}
        result = _parse_profile_text(raw, profile)
        assert result["ftp_watts"] == 0

    def test_ignores_non_bullet_lines(self):
        raw = "This is not a bullet\n- Name: Bob\n"
        profile = {"name": ""}
        result = _parse_profile_text(raw, profile)
        assert result["name"] == "Bob"

    def test_ignores_lines_without_colon(self):
        raw = "- Name without colon\n- FTP Watts: 200\n"
        profile = {"name": "", "ftp_watts": 0}
        result = _parse_profile_text(raw, profile)
        assert result["name"] == ""
        assert result["ftp_watts"] == 200

    def test_ignores_unknown_keys_not_in_profile(self):
        raw = "- Unknown Field: value\n"
        profile = {"name": ""}
        result = _parse_profile_text(raw, profile)
        assert result["name"] == ""
        # The unknown key is not in profile, so it's not set
        assert "unknown_field" not in result

    def test_returns_updated_profile(self):
        raw = "- Name: Charlie\n"
        profile = {"name": ""}
        result = _parse_profile_text(raw, profile)
        assert result is profile

    def test_parses_training_days(self):
        raw = "- Available Training Days: 5\n"
        profile = {"training_days": 0}
        result = _parse_profile_text(raw, profile)
        assert result["training_days"] == 5

    def test_parses_max_session_duration(self):
        raw = "- Max Session Duration: 120\n"
        profile = {"max_session_duration": 0}
        result = _parse_profile_text(raw, profile)
        assert result["max_session_duration"] == 120

    def test_parses_bikes(self):
        raw = "- Bike(s): Road, MTB\n"
        profile = {"bikes": ""}
        result = _parse_profile_text(raw, profile)
        assert result["bikes"] == "Road, MTB"

    def test_parses_terrain(self):
        raw = "- Terrain Notes: Hilly\n"
        profile = {"terrain": ""}
        result = _parse_profile_text(raw, profile)
        assert result["terrain"] == "Hilly"

    def test_parses_primary_goal(self):
        raw = "- Primary Goal: Endurance\n"
        profile = {"primary_goal": ""}
        result = _parse_profile_text(raw, profile)
        assert result["primary_goal"] == "Endurance"

    def test_parses_secondary_goal(self):
        raw = "- Secondary Goal: Weight Loss\n"
        profile = {"secondary_goal": ""}
        result = _parse_profile_text(raw, profile)
        assert result["secondary_goal"] == "Weight Loss"

    def test_parses_lt1_power(self):
        raw = "- LT1 Power (if known): 150\n"
        profile = {"lt1_power": 0}
        result = _parse_profile_text(raw, profile)
        assert result["lt1_power"] == 150

    def test_parses_lt2_power(self):
        raw = "- LT2 Power (if known): 250\n"
        profile = {"lt2_power": 0}
        result = _parse_profile_text(raw, profile)
        assert result["lt2_power"] == 250

    def test_parses_power_meter(self):
        raw = "- Power Meter: SRM\n"
        profile = {"power_meter": ""}
        result = _parse_profile_text(raw, profile)
        assert result["power_meter"] == "SRM"

    def test_parses_hr_monitor(self):
        raw = "- HR Monitor: Garmin HRM\n"
        profile = {"hr_monitor": ""}
        result = _parse_profile_text(raw, profile)
        assert result["hr_monitor"] == "Garmin HRM"

    def test_int_with_text_after_number(self):
        raw = "- FTP Watts: 280 watts\n"
        profile = {"ftp_watts": 0}
        result = _parse_profile_text(raw, profile)
        assert result["ftp_watts"] == 280

    def test_case_insensitive_key_matching(self):
        raw = "- ftp watts: 300\n"
        profile = {"ftp_watts": 0}
        result = _parse_profile_text(raw, profile)
        assert result["ftp_watts"] == 300

    def test_whitespace_in_value_preserved(self):
        raw = "- Name:  Extra Spaces  \n"
        profile = {"name": ""}
        result = _parse_profile_text(raw, profile)
        # line.strip() is called, so trailing whitespace is removed
        assert result["name"] == " Extra Spaces"
    def test_empty_raw(self):
        raw = ""
        profile = {"name": "unchanged"}
        result = _parse_profile_text(raw, profile)

    def test_none_input(self):
        profile = {"name": "unchanged"}
        result = _parse_profile_text(None, profile)
        assert result["name"] == "unchanged"

    def test_int_overflow_handled(self):
        # Python ints don't overflow, so this test verifies the value is parsed
        raw = "- FTP Watts: 99999999999999999999999999\n"
        profile = {"ftp_watts": 0}
        result = _parse_profile_text(raw, profile)
        assert result["ftp_watts"] == 99999999999999999999999999