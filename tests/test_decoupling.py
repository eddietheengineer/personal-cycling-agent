"""Tests for src/analytics/decoupling — aerobic decoupling (Pw:HR) analysis."""

import pytest
import numpy as np

from src.analytics.decoupling import (
    DecouplingResult,
    compute_decoupling,
    decoupling_to_dict,
)


# ── Normal operation ────────────────────────────────────────────────────────


class TestSteadyState:
    """Steady power and HR → no drift."""

    def test_steady_200w_100bpm(self):
        """Steady 200W @ 100bpm for 3600s → drift ≈ 0, recommended=True."""
        n = 3600
        power = [200.0] * n
        hr = [100.0] * n
        result = compute_decoupling("act1", power, hr)

        assert result.drift_pct == pytest.approx(0.0)
        assert result.first_half_pw_hr == pytest.approx(2.0)
        assert result.second_half_pw_hr == pytest.approx(2.0)
        assert result.increase_duration_recommended is True

    def test_steady_with_small_noise(self):
        """Small noise around steady values → drift near zero, recommended=True."""
        n = 600
        rng = np.random.default_rng(42)
        power = [200.0 + rng.uniform(-1, 1) for _ in range(n)]
        hr = [100.0 + rng.uniform(-0.5, 0.5) for _ in range(n)]
        result = compute_decoupling("act2", power, hr)

        assert abs(result.drift_pct) < 2.0  # well within 5% threshold
        assert result.increase_duration_recommended is True


class TestPositiveDrift:
    """HR rises over time (cardiac drift) → negative drift_pct."""

    def test_hr_rises_second_half(self):
        """First half 100bpm, second half 110bpm, same power → negative drift."""
        n = 200
        power = [200.0] * n
        hr = [100.0] * 100 + [110.0] * 100
        result = compute_decoupling("act3", power, hr)

        # first_half_ratio = 200/100 = 2.0
        # second_half_ratio = 200/110 ≈ 1.818
        # drift = ((1.818 - 2.0) / 2.0) * 100 ≈ -9.09%
        assert result.drift_pct < 0
        assert abs(result.drift_pct - (-9.0909)) < 0.1
        assert result.increase_duration_recommended is False

    def test_large_drift_exceeds_threshold(self):
        """HR jumps a lot → drift exceeds 5% threshold."""
        n = 100
        power = [200.0] * n
        hr = [100.0] * 50 + [130.0] * 50
        result = compute_decoupling("act4", power, hr)

        assert result.drift_pct < -5.0
        assert result.increase_duration_recommended is False


class TestNegativeDrift:
    """HR falls over time (improving fitness) → positive drift_pct."""

    def test_hr_falls_second_half(self):
        """First half 110bpm, second half 100bpm, same power → positive drift."""
        n = 200
        power = [200.0] * n
        hr = [110.0] * 100 + [100.0] * 100
        result = compute_decoupling("act5", power, hr)

        # first_half_ratio = 200/110 ≈ 1.818
        # second_half_ratio = 200/100 = 2.0
        # drift = ((2.0 - 1.818) / 1.818) * 100 ≈ +10%
        assert result.drift_pct > 0
        assert abs(result.drift_pct - 10.0) < 0.1
        # HR falling → fitness improving → recommend increase
        assert result.increase_duration_recommended is True

    def test_small_improvement_within_threshold(self):
        """HR drops slightly → drift within 5% → recommended=True."""
        n = 200
        power = [200.0] * n
        hr = [100.0] * 100 + [98.0] * 100
        result = compute_decoupling("act6", power, hr)

        # drift ≈ ((200/98 - 200/100) / (200/100)) * 100 ≈ +2.04%
        assert result.drift_pct > 0
        assert result.increase_duration_recommended is True


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestEmptyInput:
    def test_both_empty(self):
        result = compute_decoupling("act7", [], [])
        assert result.drift_pct == 0.0
        assert result.increase_duration_recommended is False

    def test_power_empty_hr_not(self):
        result = compute_decoupling("act8", [], [100.0, 100.0])
        assert result.drift_pct == 0.0
        assert result.increase_duration_recommended is False

    def test_hr_empty_power_not(self):
        result = compute_decoupling("act9", [200.0, 200.0], [])
        assert result.drift_pct == 0.0
        assert result.increase_duration_recommended is False


class TestTooFewSamples:
    def test_less_than_10(self):
        """<10 samples → early return with zeros."""
        result = compute_decoupling("act10", [200.0] * 9, [100.0] * 9)
        assert result.drift_pct == 0.0
        assert result.first_half_pw_hr == 0.0
        assert result.increase_duration_recommended is False

    def test_exactly_10(self):
        """Exactly 10 samples → should compute (5/5 split)."""
        result = compute_decoupling("act11", [200.0] * 10, [100.0] * 10)
        assert result.drift_pct == pytest.approx(0.0)
        assert result.increase_duration_recommended is True


class TestAllZeros:
    def test_all_zero_power(self):
        """All zero power → no valid ratios → early return."""
        result = compute_decoupling("act12", [0.0] * 20, [100.0] * 20)
        assert result.drift_pct == 0.0
        assert result.increase_duration_recommended is False

    def test_all_zero_hr(self):
        """All zero HR → division by zero filtered out → early return."""
        result = compute_decoupling("act13", [200.0] * 20, [0.0] * 20)
        assert result.drift_pct == 0.0
        assert result.increase_duration_recommended is False

    def test_all_zeros_both(self):
        result = compute_decoupling("act14", [0.0] * 20, [0.0] * 20)
        assert result.drift_pct == 0.0
        assert result.increase_duration_recommended is False


class TestMismatchedLengths:
    def test_power_longer(self):
        """Power has 120 samples, HR has 100 → trimmed to 100."""
        power = [200.0] * 120
        hr = [100.0] * 100
        result = compute_decoupling("act15", power, hr)
        assert result.drift_pct == pytest.approx(0.0)
        assert result.first_half_pw_hr == pytest.approx(2.0)

    def test_hr_longer(self):
        """HR has 120 samples, power has 100 → trimmed to 100."""
        power = [200.0] * 100
        hr = [100.0] * 120
        result = compute_decoupling("act16", power, hr)
        assert result.drift_pct == pytest.approx(0.0)
        assert result.first_half_pw_hr == pytest.approx(2.0)


class TestZerosInMiddle:
    def test_zeros_only_in_first_half(self):
        """Zeros in first half filtered out; second half clean → computes."""
        n = 200
        power = [0.0] * 50 + [200.0] * 50 + [200.0] * 100
        hr = [0.0] * 50 + [100.0] * 50 + [100.0] * 100
        result = compute_decoupling("act17", power, hr)
        assert result.first_half_pw_hr == pytest.approx(2.0)
    def test_zeros_only_in_second_half(self):
        """All zeros in second half → no valid second_mask → early return."""
        n = 200
        power = [200.0] * 50 + [200.0] * 50 + [0.0] * 100
        hr = [100.0] * 50 + [100.0] * 50 + [0.0] * 100
        result = compute_decoupling("act18", power, hr)
        # second half is all zeros → np.any(second_mask) is False → early return
        assert result.drift_pct == 0.0
        assert result.first_half_pw_hr == 0.0
        assert result.increase_duration_recommended is False

    def test_zeros_scattered_second_half(self):
        """Some zeros in second half (not all) → still computes from valid samples."""
        n = 200
        power = [200.0] * 100 + [200.0] * 50 + [0.0] * 50
        hr = [100.0] * 100 + [100.0] * 50 + [0.0] * 50
        result = compute_decoupling("act18b", power, hr)
        # first half: all 200/100 = 2.0; second half: 50 valid 200/100 = 2.0
        assert result.first_half_pw_hr == pytest.approx(2.0)
        assert result.second_half_pw_hr == pytest.approx(2.0)
        assert result.drift_pct == pytest.approx(0.0)
        assert result.increase_duration_recommended is True


# ── Custom threshold ────────────────────────────────────────────────────────


class TestCustomThreshold:
    def test_strict_threshold(self):
        """With threshold=1.0, even 2% negative drift (HR rising) → not recommended."""
        n = 200
        power = [200.0] * n
        hr = [100.0] * 100 + [102.0] * 100  # ~-2% drift (HR rising)
        result = compute_decoupling("act19", power, hr, drift_threshold=1.0)
        assert result.increase_duration_recommended is False

    def test_lenient_threshold(self):
        """With threshold=15.0, 9% drift → still recommended."""
        n = 200
        power = [200.0] * n
        hr = [100.0] * 100 + [110.0] * 100  # ~-9% drift
        result = compute_decoupling("act20", power, hr, drift_threshold=15.0)
        assert result.increase_duration_recommended is True


# ── Serialization ───────────────────────────────────────────────────────────


class TestDecouplingToDict:
    def test_roundtrip(self):
        """decoupling_to_dict produces correct keys and values."""
        result = DecouplingResult(
            activity_id="act21",
            first_half_pw_hr=2.0,
            second_half_pw_hr=1.8,
            drift_pct=-10.0,
            increase_duration_recommended=False,
        )
        d = decoupling_to_dict(result)
        assert d["activity_id"] == "act21"
        assert d["first_half_pw_hr"] == 2.0
        assert d["second_half_pw_hr"] == 1.8
        assert d["drift_pct"] == -10.0
        assert d["increase_duration_recommended"] is False

    def test_dict_keys_complete(self):
        """All five fields present in dict output."""
        result = DecouplingResult("x", 0.0, 0.0, 0.0, False)
        d = decoupling_to_dict(result)
        expected_keys = {
            "activity_id",
            "first_half_pw_hr",
            "second_half_pw_hr",
            "drift_pct",
            "increase_duration_recommended",
        }
        assert set(d.keys()) == expected_keys


# ── Known-value verification ────────────────────────────────────────────────


class TestKnownValues:
    def test_exact_if_one(self):
        """Steady 200W for 3600s at FTP=200 → IF=1.0 equivalent, NP≈200."""
        n = 3600
        power = [200.0] * n
        hr = [100.0] * n
        result = compute_decoupling("act22", power, hr)
        # Pw/HR ratio = 200/100 = 2.0, no drift
        assert result.first_half_pw_hr == pytest.approx(2.0)
        assert result.second_half_pw_hr == pytest.approx(2.0)
        assert result.drift_pct == pytest.approx(0.0)

    def test_known_drift_calculation(self):
        """Verify drift formula: (second - first) / first * 100."""
        # First half: P=200, HR=100 → ratio=2.0
        # Second half: P=200, HR=105 → ratio≈1.9048
        # drift = (1.9048 - 2.0) / 2.0 * 100 ≈ -4.76%
        n = 200
        power = [200.0] * n
        hr = [100.0] * 100 + [105.0] * 100
        result = compute_decoupling("act23", power, hr)
        assert abs(result.drift_pct - (-4.7619)) < 0.01
        # |drift| < 5% → recommended
        assert result.increase_duration_recommended is True


# ── Activity ID propagation ─────────────────────────────────────────────────


class TestActivityId:
    def test_id_preserved(self):
        result = compute_decoupling("my-ride-42", [200.0] * 20, [100.0] * 20)
        assert result.activity_id == "my-ride-42"

    def test_id_preserved_on_early_return(self):
        result = compute_decoupling("short-ride", [200.0] * 5, [100.0] * 5)
        assert result.activity_id == "short-ride"