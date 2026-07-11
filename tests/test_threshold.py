"""Tests for src/analytics/threshold.py — DFA-a1 threshold modeling."""

import math
import pytest

from src.analytics.threshold import (
    ThresholdResult,
    _interpolate_power_at_dfa,
    analyze_thresholds,
    analyze_batch,
    threshold_to_dict,
)


# ── _interpolate_power_at_dfa ──────────────────────────────────────────


class TestInterpolatePowerAtDfa:
    """Linear interpolation of power at a target DFA-a1 crossing."""

    def test_exact_match(self):
        """When a sample equals the target DFA exactly, return that sample's power."""
        power = [100.0, 200.0, 300.0]
        dfa = [0.9, 0.75, 0.6]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        assert result is not None
        assert result == pytest.approx(200.0)

    def test_bracketed_interpolation(self):
        """Target bracketed between two samples → linear interpolation."""
        # DFA goes 0.8 → 0.7; target 0.75 is exactly halfway
        power = [180.0, 220.0]
        dfa = [0.8, 0.7]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        assert result is not None
        assert result == pytest.approx(200.0)

    def test_bracketed_non_midpoint(self):
        """Interpolation at a non-midpoint fraction."""
        # DFA: 0.8 → 0.6; target 0.75 is 1/4 of the way
        power = [100.0, 200.0]
        dfa = [0.8, 0.6]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        assert result is not None
        assert result == pytest.approx(125.0)

    def test_never_crossed_above(self):
        """All DFA values above target → None."""
        power = [100.0, 200.0, 300.0]
        dfa = [0.9, 0.85, 0.8]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        assert result is None

    def test_never_crossed_below(self):
        """All DFA values below target → None."""
        power = [100.0, 200.0, 300.0]
        dfa = [0.6, 0.55, 0.5]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        assert result is None

    def test_mismatched_lengths_raises(self):
        """Power and DFA of different lengths → ValueError."""
        with pytest.raises(ValueError, match="same length"):
            _interpolate_power_at_dfa([100.0], [0.8, 0.7], 0.75)

    def test_flat_segment_skipped(self):
        """Two consecutive identical DFA values that equal target → skip, keep looking."""
        # First pair is flat at 0.75; second pair brackets 0.75
        power = [100.0, 100.0, 200.0, 300.0]
        dfa = [0.75, 0.75, 0.8, 0.7]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        # Flat segment (0.75, 0.75) is skipped; two crossings found:
        # (0.75, 0.8) → power=100.0, (0.8, 0.7) → power=250.0
        # Mean of all crossings = 175.0
        assert result is not None
        assert result == pytest.approx(175.0)

    def test_single_sample(self):
        """Only one sample → no pair to cross → None."""
        result = _interpolate_power_at_dfa([200.0], [0.75], 0.75)
        assert result is None

    def test_empty_arrays(self):
        """Empty arrays → no iteration → None."""
        result = _interpolate_power_at_dfa([], [], 0.75)
        assert result is None

    def test_reverse_direction(self):
        """DFA ascending (power descending) still brackets correctly."""
        power = [300.0, 100.0]
        dfa = [0.6, 0.9]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        assert result is not None
        # 0.75 is 0.75-0.6 / (0.9-0.6) = 0.5 of the way
        assert result == pytest.approx(200.0)

    def test_all_zeros(self):
        """All zero DFA values with non-zero target → None."""
        power = [100.0, 200.0, 300.0]
        dfa = [0.0, 0.0, 0.0]
        result = _interpolate_power_at_dfa(power, dfa, 0.75)
        assert result is None


# ── analyze_thresholds ─────────────────────────────────────────────────


class TestAnalyzeThresholds:
    """End-to-end threshold analysis for a single activity."""

    def test_known_dfa_crossing(self):
        """Steady-state ramp: DFA descends from 1.0 to 0.3, power 100→300."""
        power = [100.0, 200.0, 300.0]
        dfa = [1.0, 0.75, 0.5]
        result = analyze_thresholds("act-1", power, dfa)

        assert result.activity_id == "act-1"
        assert result.lt1_power == pytest.approx(200.0)
        assert result.lt2_power == pytest.approx(300.0)
        # One sample (0.5) < 0.75 → 1/3 ≈ 33.3% → audit fails
        assert result.zone2_violation_pct == pytest.approx(1 / 3, abs=1e-4)
        assert result.zone2_audit_passed is False

    def test_empty_input(self):
        """Empty power and DFA → no thresholds, audit passes."""
        result = analyze_thresholds("act-empty", [], [])

        assert result.lt1_power is None
        assert result.lt2_power is None
        assert result.zone2_violation_pct == 0.0
        assert result.zone2_audit_passed is True

    def test_mismatched_lengths_raises(self):
        """Power and DFA of different lengths → ValueError."""
        with pytest.raises(ValueError, match="must match"):
            analyze_thresholds("act-bad", [100.0, 200.0], [0.8])

    def test_all_zeros_dfa(self):
        """All-zero DFA: no crossing, every sample below LT1 → audit fails."""
        power = [100.0, 200.0, 300.0]
        dfa = [0.0, 0.0, 0.0]
        result = analyze_thresholds("act-zeros", power, dfa)

        assert result.lt1_power is None
        assert result.lt2_power is None
        assert result.zone2_violation_pct == 1.0
        assert result.zone2_audit_passed is False

    def test_single_sample(self):
        """One sample: no crossing possible; audit depends on value."""
        power = [200.0]
        dfa = [0.8]
        result = analyze_thresholds("act-one", power, dfa)

        assert result.lt1_power is None
        assert result.lt2_power is None
        # 0.8 >= 0.75 → 0 violations → passes
        assert result.zone2_violation_pct == 0.0
        assert result.zone2_audit_passed is True

    def test_steady_power_at_ftp(self):
        """Steady 200W for 3600s at DFA=0.75 (at FTP)."""
        power = [200.0] * 10
        dfa = [0.75] * 10
        result = analyze_thresholds("act-ftp", power, dfa)

        # All samples at exactly 0.75 — no crossing (flat segments skipped)
        assert result.lt1_power is None
        assert result.lt2_power is None
        # No sample strictly below 0.75 → audit passes
        assert result.zone2_violation_pct == 0.0
        assert result.zone2_audit_passed is True

    def test_zone2_audit_all_above_lt1(self):
        """All DFA above LT1 → audit passes."""
        power = [100.0, 150.0, 200.0]
        dfa = [0.9, 0.85, 0.8]
        result = analyze_thresholds("act-good", power, dfa)

        assert result.zone2_violation_pct == 0.0
        assert result.zone2_audit_passed is True

    def test_zone2_audit_all_below_lt1(self):
        """All DFA below LT1 → 100% violation → audit fails."""
        power = [100.0, 150.0, 200.0]
        dfa = [0.7, 0.65, 0.6]
        result = analyze_thresholds("act-bad", power, dfa)

        assert result.zone2_violation_pct == 1.0
        assert result.zone2_audit_passed is False

    def test_zone2_audit_exactly_at_threshold(self):
        """Exactly 10% below LT1 → passes (<= check)."""
        power = [200.0] * 10
        # 1 sample below, 9 above → 10%
        dfa = [0.74] + [0.8] * 9
        result = analyze_thresholds("act-boundary", power, dfa)

        assert result.zone2_violation_pct == pytest.approx(0.1)
        assert result.zone2_audit_passed is True

    def test_zone2_audit_just_over_threshold(self):
        """11% below LT1 → fails."""
        power = [200.0] * 100
        # 11 samples below, 89 above → 11%
        dfa = [0.74] * 11 + [0.8] * 89
        result = analyze_thresholds("act-over", power, dfa)

        assert result.zone2_violation_pct == pytest.approx(0.11)
        assert result.zone2_audit_passed is False

    def test_custom_targets(self):
        """Non-default LT1/LT2 targets."""
        power = [100.0, 200.0, 300.0]
        dfa = [1.0, 0.6, 0.3]
        result = analyze_thresholds("act-custom", power, dfa, lt1_target=0.6, lt2_target=0.3)

        assert result.lt1_power == pytest.approx(200.0)
        assert result.lt2_power == pytest.approx(300.0)

    def test_custom_zone2_threshold(self):
        """Custom violation threshold."""
        power = [200.0] * 10
        dfa = [0.74] * 5 + [0.8] * 5  # 50% below
        result = analyze_thresholds("act-custom-z2", power, dfa, zone2_violation_threshold=0.6)

        assert result.zone2_violation_pct == pytest.approx(0.5)
        assert result.zone2_audit_passed is True  # 50% <= 60%


# ── analyze_batch ──────────────────────────────────────────────────────


class TestAnalyzeBatch:
    """Batch processing of multiple activities."""

    def test_batch_processes_all(self):
        """Batch returns one result per activity."""
        data = [
            {"activity_id": "a1", "power": [100.0, 200.0], "dfa_a1": [0.9, 0.6]},
            {"activity_id": "a2", "power": [150.0, 250.0], "dfa_a1": [0.8, 0.5]},
        ]
        results = analyze_batch(data)
        assert len(results) == 2
        assert results[0].activity_id == "a1"
        assert results[1].activity_id == "a2"

    def test_batch_skips_mismatched(self):
        """Activities with mismatched arrays are skipped (logged, not raised)."""
        data = [
            {"activity_id": "ok", "power": [100.0, 200.0], "dfa_a1": [0.9, 0.6]},
            {"activity_id": "bad", "power": [100.0], "dfa_a1": [0.9, 0.6]},
        ]
        results = analyze_batch(data)
        assert len(results) == 1
        assert results[0].activity_id == "ok"

    def test_batch_empty_list(self):
        """Empty input → empty output."""
        results = analyze_batch([])
        assert results == []


# ── threshold_to_dict ──────────────────────────────────────────────────


class TestThresholdToDict:
    """Serialization of ThresholdResult."""

    def test_full_result(self):
        result = ThresholdResult(
            activity_id="act-1",
            lt1_power=200.0,
            lt2_power=300.0,
            zone2_violation_pct=0.3333,
            zone2_audit_passed=False,
        )
        d = threshold_to_dict(result)
        assert d == {
            "activity_id": "act-1",
            "lt1_power": 200.0,
            "lt2_power": 300.0,
            "zone2_violation_pct": 0.3333,
            "zone2_audit_passed": False,
        }

    def test_none_values(self):
        result = ThresholdResult(
            activity_id="act-empty",
            lt1_power=None,
            lt2_power=None,
            zone2_violation_pct=0.0,
            zone2_audit_passed=True,
        )
        d = threshold_to_dict(result)
        assert d["lt1_power"] is None
        assert d["lt2_power"] is None