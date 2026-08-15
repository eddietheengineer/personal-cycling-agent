"""Tests for src/analytics/training_load."""

import math
from datetime import date, timedelta

import pytest

from src.analytics.training_load import (
    TrainingLoadResult,
    _ema,
    compute_training_load,
    compute_training_load_history,
    training_load_to_dict,
)


# ── _ema ──────────────────────────────────────────────────────────────


class TestEma:
    def test_empty(self):
        assert _ema([], half_life=7.0) == []

    def test_single_value(self):
        assert _ema([42.0], half_life=7.0) == [42.0]

    def test_known_half_life_decay(self):
        """After one half-life of zeros, EMA decays to ~50% of initial value."""
        half_life = 7.0
        n = int(half_life)  # 7 days
        values = [100.0] + [0.0] * n
        result = _ema(values, half_life=half_life)
        # alpha = 1 - exp(-ln(2)/7) ≈ 0.0943, (1-alpha)^7 ≈ 0.5
        # EMA[7] ≈ 0.5 * 100 = 50
        assert 45 < result[-1] < 55

    def test_steady_value(self):
        """Constant input should converge to that constant."""
        values = [200.0] * 30
        result = _ema(values, half_life=7.0)
        for v in result:
            assert abs(v - 200.0) < 1e-9

    def test_all_zeros(self):
        assert _ema([0.0, 0.0, 0.0], half_life=18.0) == [0.0, 0.0, 0.0]

    def test_weight_formula(self):
        """Verify alpha = 1 - exp(-ln(2) / half_life)."""
        half_life = 18.0
        expected_alpha = 1.0 - math.exp(-math.log(2) / half_life)
        values = [0.0, 1.0]
        result = _ema(values, half_life=half_life)
        # EMA[0] = 0, EMA[1] = (1-alpha)*0 + alpha*1 = alpha
        assert abs(result[1] - expected_alpha) < 1e-15


# ── compute_training_load ─────────────────────────────────────────────


class TestComputeTrainingLoad:
    def test_empty_records(self):
        result = compute_training_load([], ftp=200)
        assert result.date == ""
        assert result.ctl == 0.0
        assert result.atl == 0.0
        assert result.tsb == 0.0
        assert result.fitness_fatigue == 0.0

    def test_single_record(self):
        records = [{"date": "2025-01-01", "tss": 100.0}]
        result = compute_training_load(records, ftp=200)
        assert result.date == "2025-01-01"
        assert result.ctl == 100.0
        assert result.atl == 100.0
        assert result.tsb == 0.0
        assert result.fitness_fatigue == 1.0

    def test_steady_200w_at_ftp(self):
        """3600 seconds at 200W with FTP=200 → IF=1.0, NP≈200.
        TSS = power * duration / FTP * 100 = 200 * 3600 / 200 / 3600 * 100 = 100.
        Steady 100 TSS/day for 30 days → CTL and ATL both converge to 100."""
        records = []
        for i in range(30):
            d = (date(2025, 1, 1) + timedelta(days=i)).isoformat()
            records.append({"date": d, "tss": 100.0})
        result = compute_training_load(records, ftp=200)
        assert abs(result.ctl - 100.0) < 0.01
        assert abs(result.atl - 100.0) < 0.01
        assert abs(result.tsb) < 0.01
        assert abs(result.fitness_fatigue - 1.0) < 0.01

    def test_all_zeros_tss(self):
        records = [{"date": "2025-01-01", "tss": 0.0}]
        result = compute_training_load(records, ftp=200)
        assert result.ctl == 0.0
        assert result.atl == 0.0
        assert result.fitness_fatigue == 0.0  # 0/0 → 0

    def test_ftp_zero(self):
        """FTP is not used for division in this function; TSS is precomputed."""
        records = [{"date": "2025-01-01", "tss": 50.0}]
        result = compute_training_load(records, ftp=0)
        assert result.ctl == 50.0
        assert result.atl == 50.0

    def test_unsorted_records(self):
        """Function should sort records by date internally."""
        records = [
            {"date": "2025-01-03", "tss": 300.0},
            {"date": "2025-01-01", "tss": 100.0},
            {"date": "2025-01-02", "tss": 200.0},
        ]
        result = compute_training_load(records, ftp=200)
        assert result.date == "2025-01-03"

    def test_increasing_tss(self):
        """Rising TSS: ATL (7-day) rises faster than CTL (42-day)."""
        records = []
        for i in range(14):
            d = (date(2025, 1, 1) + timedelta(days=i)).isoformat()
            records.append({"date": d, "tss": float(i * 50 + 50)})
        result = compute_training_load(records, ftp=200)
        # ATL (7-day half-life) responds faster to rising TSS than CTL (42-day).
        # With correct EMA: alpha_7 ≈ 0.094, alpha_42 ≈ 0.016.
        # ATL gives more weight to recent high values, so ATL > CTL on rising trend.
        assert result.ctl < result.atl


# ── compute_training_load_history ─────────────────────────────────────


class TestComputeTrainingLoadHistory:
    def test_empty(self):
        assert compute_training_load_history([]) == []

    def test_single_day(self):
        records = [{"date": "2025-01-01", "tss": 100.0}]
        result = compute_training_load_history(records)
        assert len(result) == 1
        assert result[0]["date"] == "2025-01-01"
        assert result[0]["ctl"] == 100.0
        assert result[0]["atl"] == 100.0
        assert result[0]["tsb"] == 0.0
        assert result[0]["fb"] == 1.0

    def test_gap_filling(self):
        """Missing days between records should be filled with TSS=0."""
        records = [
            {"date": "2025-01-01", "tss": 100.0},
            {"date": "2025-01-05", "tss": 100.0},
        ]
        result = compute_training_load_history(records)
        # 5 days inclusive: Jan 1, 2, 3, 4, 5
        assert len(result) == 5
        assert result[0]["date"] == "2025-01-01"
        assert result[4]["date"] == "2025-01-05"
        # Gap days (index 1-3) had TSS=0, so EMA should decay
        assert result[1]["ctl"] < result[0]["ctl"]

    def test_consecutive_days(self):
        """No gaps → one entry per input record."""
        records = []
        for i in range(3):
            d = (date(2025, 1, 1) + timedelta(days=i)).isoformat()
            records.append({"date": d, "tss": 50.0})
        result = compute_training_load_history(records)
        assert len(result) == 3

    def test_all_zeros_tss(self):
        records = [
            {"date": "2025-01-01", "tss": 0.0},
            {"date": "2025-01-02", "tss": 0.0},
        ]
        result = compute_training_load_history(records)
        assert len(result) == 2
        for entry in result:
            assert entry["ctl"] == 0.0
            assert entry["atl"] == 0.0
            assert entry["fb"] == 0.0

    def test_result_keys(self):
        records = [{"date": "2025-01-01", "tss": 100.0}]
        result = compute_training_load_history(records)
        expected_keys = {"date", "ctl", "atl", "tsb", "fb"}
        assert set(result[0].keys()) == expected_keys

    def test_tsb_is_ctl_minus_atl(self):
        records = []
        for i in range(10):
            d = (date(2025, 1, 1) + timedelta(days=i)).isoformat()
            records.append({"date": d, "tss": 100.0})
        result = compute_training_load_history(records)
        for entry in result:
            assert abs(entry["tsb"] - (entry["ctl"] - entry["atl"])) < 1e-12

    def test_fb_zero_when_atl_zero(self):
        """When ATL is zero (all-zero TSS), FB should be 0.0, not NaN."""
        records = [{"date": "2025-01-01", "tss": 0.0}]
        result = compute_training_load_history(records)
        assert result[0]["fb"] == 0.0
        assert not math.isnan(result[0]["fb"])


# ── training_load_to_dict ─────────────────────────────────────────────


class TestTrainingLoadToDict:
    def test_serialize(self):
        result = TrainingLoadResult(
            date="2025-01-01", ctl=100.0, atl=80.0, tsb=20.0, fitness_fatigue=1.25, acwr=0.8
        )
        d = training_load_to_dict(result)
        assert d == {
            "date": "2025-01-01",
            "ctl": 100.0,
            "atl": 80.0,
            "tsb": 20.0,
            "fitness_fatigue": 1.25,
            "acwr": 0.8,
        }