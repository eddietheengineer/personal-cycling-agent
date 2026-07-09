"""Tests for src/analytics/readiness.py"""

import math

import pytest

from src.analytics.readiness import (
    ReadinessResult,
    ReadinessState,
    _compute_bands,
    assess_readiness,
    assess_all_dates,
    readiness_to_dict,
)


# ── _compute_bands ──────────────────────────────────────────────────────


class TestComputeBands:
    """Unit tests for _compute_bands."""

    def test_known_values(self):
        """Mean, std, and bands for a known distribution."""
        values = [50, 55, 60, 65, 70]
        mean, std, lower, upper = _compute_bands(values)
        assert mean == pytest.approx(60.0)
        # sample std (ddof=1) of [50,55,60,65,70] ≈ 7.906
        assert std == pytest.approx(7.905694150420948)
        assert lower == pytest.approx(60.0 - 0.75 * std)
        assert upper == pytest.approx(60.0 + 0.75 * std)

    def test_single_value_std_is_zero(self):
        """With one sample, std must be 0.0 and bands collapse to mean."""
        mean, std, lower, upper = _compute_bands([42.0])
        assert mean == pytest.approx(42.0)
    def test_empty_list(self):
        """Empty input produces NaN mean (numpy behavior)."""
        mean, std, lower, upper = _compute_bands([])
        assert math.isnan(mean)
        assert std == 0.0  # len(arr) <= 1 → std=0

    def test_custom_window_ignored(self):
        """_compute_bands ignores window for the stats (uses all values)."""
        values = [10, 20, 30]
        mean, std, lower, upper = _compute_bands(values, window=100)
        assert mean == pytest.approx(20.0)

    def test_all_identical_values(self):
        """All same values → std=0, bands equal mean."""
        values = [50.0] * 10
        mean, std, lower, upper = _compute_bands(values)
        assert mean == pytest.approx(50.0)
        assert std == 0.0
        assert lower == pytest.approx(50.0)
        assert upper == pytest.approx(50.0)

    def test_negative_values(self):
        """Negative values are handled correctly."""
        values = [-10, -5, 0, 5, 10]
        mean, std, lower, upper = _compute_bands(values)
        assert mean == pytest.approx(0.0)


# ── assess_readiness ────────────────────────────────────────────────────


def _make_records(dates, rmssd=55.0, rhr=50.0):
    """Helper to create wellness record dicts."""
    return [
        {"date": d, "rmssd": rmssd, "resting_hr": rhr}
        for d in dates
    ]


class TestAssessReadinessCoping:
    """Coping state: values within normal bands."""

    def test_coping_state(self):
        """When RMSSD and RHR are within bands, state is COPING."""
        # 30 days of baseline + today, all identical → bands collapse to the value
        baseline = _make_records([f"2025-01-{d:02d}" for d in range(1, 31)], rmssd=55.0, rhr=50.0)
        today = {"date": "2025-01-31", "rmssd": 55.0, "resting_hr": 50.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.COPING
        assert "Coping well" in result.recommendation

    def test_coping_with_varied_baseline(self):
        """Coping when today's values fall within the computed bands."""
        # Baseline: RMSSD varies around 55, RHR around 50
        baseline = []
        for d in range(1, 31):
            baseline.append({
                "date": f"2025-01-{d:02d}",
                "rmssd": 50 + d % 10,  # 50..59
                "resting_hr": 48 + d % 5,  # 48..52
            })
        today = {"date": "2025-01-31", "rmssd": 55.0, "resting_hr": 50.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.COPING


class TestAssessReadinessSympatheticStress:
    """Sympathetic stress: low RMSSD + high RHR."""

    def test_sympathetic_stress(self):
        """Low RMSSD below band AND high RHR above band → sympathetic stress."""
        baseline = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 31)],
            rmssd=55.0,
            rhr=50.0,
        )
        # All identical baseline → bands collapse to 55/50
        today = {"date": "2025-01-31", "rmssd": 30.0, "resting_hr": 70.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.SYMPATHETIC_STRESS
        assert "Sympathetic stress" in result.recommendation

    def test_sympathetic_stress_with_varied_baseline(self):
        """Sympathetic stress with a realistic varied baseline."""
        baseline = []
        for d in range(1, 31):
            baseline.append({
                "date": f"2025-01-{d:02d}",
                "rmssd": 55.0 + (d % 5),  # 55..59
                "resting_hr": 50.0 + (d % 3),  # 50..52
            })
        # RMSSD well below lower band, RHR well above upper band
        today = {"date": "2025-01-31", "rmssd": 25.0, "resting_hr": 65.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.SYMPATHETIC_STRESS


class TestAssessReadinessParasympatheticHyperactivity:
    """Parasympathetic hyperactivity: high RMSSD + low RHR."""

    def test_parasympathetic_hyperactivity(self):
        """High RMSSD above band AND low RHR below band → parasympathetic hyperactivity."""
        baseline = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 31)],
            rmssd=55.0,
            rhr=50.0,
        )
        today = {"date": "2025-01-31", "rmssd": 80.0, "resting_hr": 30.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.PARASYMPATHETIC_HYPERACTIVITY
        assert "Parasympathetic hyperactivity" in result.recommendation


# ── Edge cases ──────────────────────────────────────────────────────────


class TestAssessReadinessEdgeCases:

    def test_empty_records_raises(self):
        """Empty input raises ValueError."""
        with pytest.raises(ValueError, match="No wellness records"):
            assess_readiness([])

    def test_missing_rmssd_only(self):
        """RMSSD missing, RHR within bands → coping (with note about no HRV)."""
        baseline = []
        for d in range(1, 31):
            baseline.append({
                "date": f"2025-01-{d:02d}",
                "rmssd": None,
                "resting_hr": 50.0,
            })
        today = {"date": "2025-01-31", "rmssd": None, "resting_hr": 50.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.COPING
        assert "no HRV data" in result.recommendation

    def test_missing_rhr_only(self):
        """RHR missing, RMSSD within bands → coping."""
        baseline = []
        for d in range(1, 31):
            baseline.append({
                "date": f"2025-01-{d:02d}",
                "rmssd": 55.0,
                "resting_hr": None,
            })
        today = {"date": "2025-01-31", "rmssd": 55.0, "resting_hr": None}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.COPING

    def test_both_missing_raises(self):
        """Both RMSSD and RHR missing for target date raises ValueError."""
        baseline = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 31)],
            rmssd=55.0,
            rhr=50.0,
        )
        today = {"date": "2025-01-31", "rmssd": None, "resting_hr": None}
        records = baseline + [today]

        with pytest.raises(ValueError, match="Both RMSSD and resting_hr are missing"):
            assess_readiness(records)

    def test_no_baseline_data(self):
        """With no baseline records, bands default to 0.0."""
        today = {"date": "2025-01-01", "rmssd": 55.0, "resting_hr": 50.0}
        result = assess_readiness([today])
        assert result.rmssd_mean == 0.0
        assert result.rmssd_std == 0.0
        assert result.rmssd_lower_band == 0.0
        assert result.rmssd_upper_band == 0.0
        assert result.rhr_mean == 0.0
        assert result.rhr_std == 0.0
        assert result.rhr_lower_band == 0.0
        assert result.rhr_upper_band == 0.0

    def test_single_record(self):
        """A single record with no baseline → bands are zero, RMSSD > 0 upper band."""
        today = {"date": "2025-01-01", "rmssd": 55.0, "resting_hr": 50.0}
        result = assess_readiness([today])
        # With no baseline, bands are 0. RMSSD=55 > upper=0, RHR=50 > upper=0.
        # rmssd_above=True, rhr_below=False → not parasympathetic.
        # rmssd_below=False, rhr_above=True → not sympathetic.
        # Falls through to COPING.
        assert result.state == ReadinessState.COPING

    def test_target_date_not_found_raises(self):
        """Requesting a date not in records raises ValueError."""
        records = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 6)],
            rmssd=55.0,
            rhr=50.0,
        )
        with pytest.raises(ValueError, match="No wellness record found for date"):
            assess_readiness(records, target_date="2025-01-10")

    def test_default_target_date_is_most_recent(self):
        """When target_date is None, the most recent record is used."""
        records = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 6)],
            rmssd=55.0,
            rhr=50.0,
        )
        result = assess_readiness(records)
        assert result.date == "2025-01-05"

    def test_confidence_high_with_enough_data(self):
        """Confidence is 'high' when >= 7 RMSSD and >= 7 RHR baseline values."""
        baseline = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 15)],
            rmssd=55.0,
            rhr=50.0,
        )
        today = {"date": "2025-01-15", "rmssd": 55.0, "resting_hr": 50.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.confidence == "high"

    def test_confidence_low_with_few_data(self):
        """Confidence is 'low' when < 7 baseline values."""
        baseline = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 5)],
            rmssd=55.0,
            rhr=50.0,
        )
        today = {"date": "2025-01-05", "rmssd": 55.0, "resting_hr": 50.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.confidence == "low"

    def test_sympathetic_stress_missing_rmssd(self):
        """RHR above baseline with no RMSSD → sympathetic stress."""
        baseline = []
        for d in range(1, 31):
            baseline.append({
                "date": f"2025-01-{d:02d}",
                "rmssd": None,
                "resting_hr": 50.0,
            })
        today = {"date": "2025-01-31", "rmssd": None, "resting_hr": 70.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.SYMPATHETIC_STRESS
        assert "no HRV data" in result.recommendation

    def test_parasympathetic_hyperactivity_missing_rmssd(self):
        """RHR below baseline with no RMSSD → parasympathetic hyperactivity."""
        baseline = []
        for d in range(1, 31):
            baseline.append({
                "date": f"2025-01-{d:02d}",
                "rmssd": None,
                "resting_hr": 50.0,
            })
        today = {"date": "2025-01-31", "rmssd": None, "resting_hr": 30.0}
        records = baseline + [today]

        result = assess_readiness(records)
        assert result.state == ReadinessState.PARASYMPATHETIC_HYPERACTIVITY
        assert "no HRV data" in result.recommendation


# ── readiness_to_dict ───────────────────────────────────────────────────


class TestReadinessToDict:
    """Serialization of ReadinessResult to dict."""

    def test_serialization(self):
        """readiness_to_dict produces a correct dict with expected keys."""
        result = ReadinessResult(
            date="2025-01-31",
            rmssd=55.0,
            resting_hr=50.0,
            rmssd_mean=55.0,
            rmssd_std=5.0,
            rhr_mean=50.0,
            rhr_std=3.0,
            rmssd_lower_band=51.25,
            rmssd_upper_band=58.75,
            rhr_lower_band=47.75,
            rhr_upper_band=52.25,
            state=ReadinessState.COPING,
            recommendation="Coping well.",
            confidence="high",
        )
        d = readiness_to_dict(result)
        assert d["date"] == "2025-01-31"
        assert d["rmssd"] == 55.0
        assert d["resting_hr"] == 50.0
        assert d["state"] == "coping"
        assert d["confidence"] == "high"
        assert isinstance(d["rmssd_band"], list)
        assert len(d["rmssd_band"]) == 2
        assert isinstance(d["rhr_band"], list)
        assert len(d["rhr_band"]) == 2

    def test_serialization_with_none_values(self):
        """Serialization handles None RMSSD/RHR gracefully."""
        result = ReadinessResult(
            date="2025-01-31",
            rmssd=None,
            resting_hr=50.0,
            rmssd_mean=0.0,
            rmssd_std=0.0,
            rhr_mean=50.0,
            rhr_std=0.0,
            rmssd_lower_band=0.0,
            rmssd_upper_band=0.0,
            rhr_lower_band=50.0,
            rhr_upper_band=50.0,
            state=ReadinessState.COPING,
            recommendation="RHR within normal bands.",
            confidence="low",
        )
        d = readiness_to_dict(result)
        assert d["rmssd"] is None


# ── assess_all_dates ────────────────────────────────────────────────────


class TestAssessAllDates:
    """Integration-level: assess readiness for every date."""

    def test_returns_result_per_date(self):
        """One result per unique date in records."""
        records = _make_records(
            [f"2025-01-{d:02d}" for d in range(1, 6)],
            rmssd=55.0,
            rhr=50.0,
        )
        results = assess_all_dates(records)
        assert len(results) == 5

    def test_skips_unparseable_dates(self):
        """assess_all_dates logs warning and skips dates that fail."""
        records = [
            {"date": "2025-01-01", "rmssd": None, "resting_hr": None},
        ]
        results = assess_all_dates(records)
        # The single record has both None → ValueError → skipped
        assert len(results) == 0