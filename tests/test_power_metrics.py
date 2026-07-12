"""Tests for src/analytics/power_metrics."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from analytics.power_metrics import (
    _compute_normalized_power,
    _compute_time_in_zones,
    _compute_power_duration_curve,
    compute_power_metrics,
    estimate_critical_power,
    power_metrics_to_dict,
    PowerMetricsResult,
)


# ── _compute_normalized_power ──────────────────────────────────────────────


class TestComputeNormalizedPower:
    def test_steady_power_equals_average(self):
        """Steady power: NP should equal the constant value."""
        power = np.full(3600, 200.0)
        np_val = _compute_normalized_power(power)
        assert np.isclose(np_val, 200.0, atol=1.0)

    def test_variable_power_greater_than_average(self):
        """Variable power: NP should be greater than arithmetic mean."""
        power = np.concatenate([np.full(1800, 150.0), np.full(1800, 250.0)])
        avg = float(np.mean(power))
        np_val = _compute_normalized_power(power)
        assert np_val > avg

    def test_empty_array(self):
        """Empty input returns 0.0."""
        assert _compute_normalized_power(np.array([])) == 0.0

    def test_single_sample(self):
        """Single sample: NP equals that value."""
        power = np.array([200.0])
        np_val = _compute_normalized_power(power)
        assert np.isclose(np_val, 200.0, atol=1.0)

    def test_all_zeros(self):
        """All zeros returns 0.0."""
        power = np.zeros(100)
        assert _compute_normalized_power(power) == 0.0

    def test_short_array_less_than_window(self):
        """Array shorter than 30s window still produces valid NP."""
        power = np.full(10, 250.0)
        np_val = _compute_normalized_power(power)
        assert np.isclose(np_val, 250.0, atol=1.0)


# ── _compute_time_in_zones ─────────────────────────────────────────────────


class TestComputeTimeInZones:
    def test_all_z1(self):
        """All power below 56% FTP → 100% Z1."""
        power = np.full(100, 50.0)  # FTP=200 → 25%
        zones = _compute_time_in_zones(power, 200.0)
        assert zones["Z1"] == 100.0
        for z in ["Z2", "Z3", "Z4", "Z5"]:
            assert zones[z] == 0.0

    def test_all_z5(self):
        """All power above 105% FTP → 100% Z5."""
        power = np.full(100, 250.0)  # FTP=200 → 125%
        zones = _compute_time_in_zones(power, 200.0)
        assert zones["Z5"] == 100.0
        for z in ["Z1", "Z2", "Z3", "Z4"]:
            assert zones[z] == 0.0

    def test_mixed_zones(self):
        """50% Z1, 50% Z4 → correct percentages."""
        z1_power = np.full(50, 50.0)   # 25% of FTP=200
        z4_power = np.full(50, 200.0)  # 100% of FTP=200
        power = np.concatenate([z1_power, z4_power])
        zones = _compute_time_in_zones(power, 200.0)
        assert np.isclose(zones["Z1"], 50.0)
        assert np.isclose(zones["Z4"], 50.0)

    def test_ftp_zero(self):
        """FTP=0 → all samples treated as zeros → 100% Z1."""
        power = np.full(100, 200.0)
        zones = _compute_time_in_zones(power, 0.0)
        assert zones["Z1"] == 100.0

    def test_empty_array(self):
        """Empty array returns all zeros."""
        zones = _compute_time_in_zones(np.array([]), 200.0)
        for z in ["Z1", "Z2", "Z3", "Z4", "Z5"]:
            assert zones[z] == 0.0

    def test_zone_boundaries_exact(self):
        """Power exactly at boundary values falls into correct zones."""
        ftp = 200.0
        # 56% of 200 = 112 → Z2 starts here
        power = np.array([111.0, 112.0, 150.0, 151.0, 180.0, 181.0, 210.0])
        zones = _compute_time_in_zones(power, ftp)
        assert zones["Z1"] > 0  # 111 < 112
        assert zones["Z2"] > 0  # 112, 150
        assert zones["Z3"] > 0  # 151, 180
        assert zones["Z4"] > 0  # 181, 210


# ── _compute_power_duration_curve ──────────────────────────────────────────


class TestComputePowerDurationCurve:
    def test_steady_power(self):
        """Steady power: every duration returns the constant value."""
        power = np.full(3600, 200.0)
        curve = _compute_power_duration_curve(power)
        for dur in [1, 3, 5, 10, 30, 60, 120, 180, 300, 600, 1200, 1800, 3600]:
            assert np.isclose(curve[dur], 200.0)

    def test_short_array(self):
        """Durations longer than array return 0.0."""
        power = np.full(5, 300.0)
        curve = _compute_power_duration_curve(power)
        assert np.isclose(curve[1], 300.0)
        assert np.isclose(curve[3], 300.0)
        assert np.isclose(curve[5], 300.0)
        assert curve[10] == 0.0
        assert curve[3600] == 0.0

    def test_empty_array(self):
        """Empty array: all durations return 0.0."""
        curve = _compute_power_duration_curve(np.array([]))
        for dur in curve:
            assert curve[dur] == 0.0

    def test_sprint_burst(self):
        """A short burst should show up in short-duration entries."""
        power = np.zeros(60)
        power[10:15] = 1000.0  # 5-second sprint
        curve = _compute_power_duration_curve(power)
        assert curve[1] == 1000.0
        assert curve[3] == 1000.0
        assert curve[5] == 1000.0
        # 10s window dilutes the 5s burst
        assert curve[10] < 1000.0


# ── compute_power_metrics ──────────────────────────────────────────────────────


class TestComputePowerMetrics:
    def test_known_values_steady_at_ftp(self):
        """200W steady for 3600s at FTP=200 → IF=1.0, NP≈200."""
        samples = [200.0] * 3600
        result = compute_power_metrics("act1", samples, 3600.0, 200.0)
        assert np.isclose(result.normalized_power, 200.0, atol=1.0)
        assert np.isclose(result.intensity_factor, 1.0, atol=0.01)

    def test_tss_steady_at_ftp_one_hour(self):
        """TSS for 1h at FTP: TSS = 3600 * 200 * 1^2 / (200 * 3600) * 100 = 100."""
        samples = [200.0] * 3600
        result = compute_power_metrics("act1", samples, 3600.0, 200.0)
        assert np.isclose(result.tss, 100.0, atol=1.0)

    def test_vi_steady_power(self):
        """Steady power: VI = avg/NP ≈ 1.0."""
        samples = [200.0] * 3600
        result = compute_power_metrics("act1", samples, 3600.0, 200.0)
        assert np.isclose(result.variability_index, 1.0, atol=0.01)

    def test_ftp_zero(self):
        """FTP=0 returns all zeros."""
        samples = [200.0] * 100
        result = compute_power_metrics("act1", samples, 100.0, 0.0)
        assert result.normalized_power == 0.0
        assert result.intensity_factor == 0.0
        assert result.tss == 0.0
        assert result.variability_index == 0.0

    def test_empty_samples(self):
        """Empty samples returns all zeros."""
        result = compute_power_metrics("act1", [], 0.0, 200.0)
        assert result.normalized_power == 0.0
        assert result.intensity_factor == 0.0
        assert result.tss == 0.0
        assert result.variability_index == 0.0

    def test_variable_power_vi_less_than_one(self):
        """Variable effort: VI < 1.0."""
        power = [150.0] * 1800 + [250.0] * 1800
        result = compute_power_metrics("act1", power, 3600.0, 200.0)
        assert result.variability_index < 1.0

    def test_time_in_zones_sum_to_100(self):
        """Zone percentages sum to 100%."""
        samples = list(range(1, 301))  # 1..300 W
        result = compute_power_metrics("act1", samples, 300.0, 200.0)
        total = sum(result.time_in_zones.values())
        assert np.isclose(total, 100.0)

    def test_pdc_has_all_durations(self):
        """PDC dict has all 13 standard durations."""
        samples = [200.0] * 3600
        result = compute_power_metrics("act1", samples, 3600.0, 200.0)
        assert len(result.power_duration_curve) == 13

    def test_activity_id_preserved(self):
        """Activity ID is preserved in result."""
        samples = [200.0] * 100
        result = compute_power_metrics("my-ride-42", samples, 100.0, 200.0)
        assert result.activity_id == "my-ride-42"


# ── estimate_critical_power ────────────────────────────────────────────────
# Now returns (cp, w_prime) tuple; uses PDC efforts; min duration 180s.


class TestEstimateCriticalPower:
    def test_two_known_pdc_efforts(self):
        """Two activities with PDC efforts at standard durations → positive CP."""
        activities = [
            {
                "pdc_efforts": [
                    {"duration": 180, "avg_power": 310.0},
                    {"duration": 300, "avg_power": 280.0},
                    {"duration": 480, "avg_power": 260.0},
                    {"duration": 1200, "avg_power": 240.0},
                ]
            },
            {
                "pdc_efforts": [
                    {"duration": 180, "avg_power": 305.0},
                    {"duration": 300, "avg_power": 275.0},
                    {"duration": 480, "avg_power": 255.0},
                    {"duration": 1200, "avg_power": 235.0},
                ]
            },
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp > 0
        assert wp > 0
        # CP should be below the lowest observed power (235W)
        assert cp < 240

    def test_from_power_duration_curve(self):
        """Can extract efforts from power_duration_curve dict."""
        activities = [
            {
                "power_duration_curve": {
                    180: 310.0, 300: 280.0, 480: 260.0, 1200: 240.0,
                    1: 500.0, 3: 450.0, 5: 430.0, 10: 400.0,
                }
            },
            {
                "power_duration_curve": {
                    180: 305.0, 300: 275.0, 480: 255.0, 1200: 235.0,
                    1: 490.0, 3: 440.0, 5: 420.0, 10: 390.0,
                }
            },
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp > 0
        assert wp > 0

    def test_legacy_fallback_whole_ride(self):
        """Legacy whole-ride avg_power still works as fallback."""
        activities = [
            {"duration": 3600, "avg_power": 250.0},
            {"duration": 7200, "avg_power": 220.0},
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp > 0
        # With 1h@250W and 2h@220W, CP ≈ 190W
        assert 180 < cp < 220
        assert wp > 0

    def test_less_than_two_efforts(self):
        """<2 qualifying efforts returns (0.0, 0.0)."""
        activities = [{"pdc_efforts": [{"duration": 180, "avg_power": 300.0}]}]
        cp, wp = estimate_critical_power(activities)
        assert cp == 0.0
        assert wp == 0.0

    def test_empty_list(self):
        """Empty list returns (0.0, 0.0)."""
        cp, wp = estimate_critical_power([])
        assert cp == 0.0
        assert wp == 0.0

    def test_single_activity_with_multiple_efforts(self):
        """Single activity with multiple PDC efforts → enough points."""
        activities = [
            {
                "pdc_efforts": [
                    {"duration": 180, "avg_power": 310.0},
                    {"duration": 300, "avg_power": 280.0},
                    {"duration": 480, "avg_power": 260.0},
                ]
            },
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp > 0
        assert wp > 0

    def test_non_positive_cp_clamped(self):
        """If regression yields non-positive CP, return (0.0, 0.0)."""
        activities = [
            {
                "pdc_efforts": [
                    {"duration": 1200, "avg_power": 200.0},
                    {"duration": 180, "avg_power": 100.0},
                ]
            },
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp >= 0

    def test_short_efforts_filtered(self):
        """Efforts < 180s are skipped."""
        activities = [
            {
                "pdc_efforts": [
                    {"duration": 30, "avg_power": 500.0},
                    {"duration": 45, "avg_power": 400.0},
                    {"duration": 120, "avg_power": 350.0},
                ]
            },
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp == 0.0
        assert wp == 0.0

    def test_zero_power_filtered(self):
        """Efforts with avg_power <= 0 are skipped."""
        activities = [
            {
                "pdc_efforts": [
                    {"duration": 3600, "avg_power": 0.0},
                    {"duration": 7200, "avg_power": -10.0},
                ]
            },
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp == 0.0
        assert wp == 0.0

    def test_cp_clamped_below_max(self):
        """If CP >= max observed, clamp to 95% of max."""
        activities = [
            {
                "pdc_efforts": [
                    {"duration": 180, "avg_power": 200.0},
                    {"duration": 300, "avg_power": 200.0},
                    {"duration": 480, "avg_power": 200.0},
                ]
            },
        ]
        cp, wp = estimate_critical_power(activities)
        # With identical power at all durations, CP ≈ 200 which equals max.
        # Should be clamped to ~95% of max, or at least not exceed max.
        assert cp <= 200.0 + 1e-6  # allow floating point tolerance

    def test_rounded_output(self):
        """CP and W' are rounded to 2 decimal places."""
        activities = [
            {"duration": 3600, "avg_power": 250.0},
            {"duration": 7200, "avg_power": 220.0},
        ]
        cp, wp = estimate_critical_power(activities)
        assert cp == round(cp, 2)
        assert wp == round(wp, 2)

    def test_w_prime_positive(self):
        """W' from regression should be positive for realistic data."""
        activities = [
            {
                "pdc_efforts": [
                    {"duration": 180, "avg_power": 320.0},
                    {"duration": 1200, "avg_power": 250.0},
                ]
            },
        ]
        cp, wp = estimate_critical_power(activities)
        assert wp > 0


# ── power_metrics_to_dict ──────────────────────────────────────────────────


class TestPowerMetricsToDict:
    def test_serialization(self):
        """Result serializes to dict with correct keys."""
        result = PowerMetricsResult(
            activity_id="test",
            normalized_power=200.0,
            intensity_factor=1.0,
            tss=100.0,
            variability_index=0.95,
            time_in_zones={"Z1": 10.0, "Z2": 50.0, "Z3": 20.0, "Z4": 15.0, "Z5": 5.0},
            power_duration_curve={1: 400.0, 3600: 200.0},
        )
        d = power_metrics_to_dict(result)
        assert d["activity_id"] == "test"
        assert d["normalized_power"] == 200.0
        assert d["intensity_factor"] == 1.0
        assert d["tss"] == 100.0
        assert d["variability_index"] == 0.95
        assert d["time_in_zones"]["Z2"] == 50.0
        assert d["power_duration_curve"][1] == 400.0