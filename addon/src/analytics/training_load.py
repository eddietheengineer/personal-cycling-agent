"""
Training Load Analytics.

Computes chronic/acute training load metrics using exponential moving
averages of daily TSS (Training Stress Score), following the TrainingPeaks
model with configurable half-lives:

- CTL (Chronic Training Load): 30-day EMA of TSS, half-life 18 days
- ATL (Acute Training Load): 7-day EMA of TSS, half-life 7 days
- TSB (Training Stress Balance): CTL - ATL
- FB (Fitness-Fatigue ratio): CTL / ATL
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


@dataclass
class TrainingLoadResult:
    """Training load metrics for a single date."""

    date: str
    ctl: float  # Chronic Training Load (30-day EMA)
    atl: float  # Acute Training Load (7-day EMA)
    tsb: float  # Training Stress Balance (CTL - ATL)
    fitness_fatigue: float  # Fitness-Fatigue ratio (CTL / ATL)


def _ema(values: list[float], half_life: float) -> list[float]:
    """
    Compute exponential moving average with a given half-life in days.

    Uses the weight formula: w = exp(-ln(2) / half_life)
    EMA[i] = (1 - w) * EMA[i-1] + w * value[i]

    For the first value, EMA equals the value itself.
    """
    if not values:
        return []

    w = math.exp(-math.log(2) / half_life)
    ema = [values[0]]

    for i in range(1, len(values)):
        ema.append((1 - w) * ema[-1] + w * values[i])

    return ema


def compute_training_load(
    tss_records: list[dict[str, Any]], ftp: float
) -> TrainingLoadResult:
    """
    Compute training load metrics for the most recent date in the data.

    Args:
        tss_records: list of {date: str, tss: float} sorted by date ascending.
            Each entry represents one day's total TSS.
        ftp: Functional Threshold Power in watts (used for context;
            TSS values are already computed).

    Returns:
        TrainingLoadResult with CTL, ATL, TSB, and fitness_fatigue for the
        most recent date.
    """
    if not tss_records:
        return TrainingLoadResult(
            date="", ctl=0.0, atl=0.0, tsb=0.0, fitness_fatigue=0.0
        )

    # Sort by date to ensure chronological order
    sorted_records = sorted(tss_records, key=lambda r: r["date"])

    tss_values = [r["tss"] for r in sorted_records]
    dates = [r["date"] for r in sorted_records]

    ctl = _ema(tss_values, half_life=18.0)
    atl = _ema(tss_values, half_life=7.0)

    last_idx = len(sorted_records) - 1
    ctl_val = ctl[last_idx]
    atl_val = atl[last_idx]
    tsb_val = ctl_val - atl_val
    fb_val = ctl_val / atl_val if atl_val > 0 else 0.0

    return TrainingLoadResult(
        date=dates[last_idx],
        ctl=ctl_val,
        atl=atl_val,
        tsb=tsb_val,
        fitness_fatigue=fb_val,
    )


def compute_training_load_history(
    tss_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Compute CTL/ATL/TSB/FB for every date in the data range.

    Fills gaps between recorded dates with zero TSS so the EMA
    reflects true calendar-day progression.

    Args:
        tss_records: list of {date: str, tss: float}. Dates may have gaps.

    Returns:
        List of {date, ctl, atl, tsb, fb} dicts, one per calendar day
        from the earliest to the latest date in the input.
    """
    if not tss_records:
        return []

    # Sort and build a date-indexed TSS map
    sorted_records = sorted(tss_records, key=lambda r: r["date"])

    start_date = date.fromisoformat(sorted_records[0]["date"])
    end_date = date.fromisoformat(sorted_records[-1]["date"])

    tss_by_date: dict[date, float] = {}
    for r in sorted_records:
        tss_by_date[date.fromisoformat(r["date"])] = r["tss"]

    # Build daily TSS series (zero-fill gaps)
    delta = (end_date - start_date).days
    daily_tss = []
    daily_dates = []

    current = start_date
    for _ in range(delta + 1):
        daily_tss.append(tss_by_date.get(current, 0.0))
        daily_dates.append(current.isoformat())
        current += timedelta(days=1)

    ctl = _ema(daily_tss, half_life=18.0)
    atl = _ema(daily_tss, half_life=7.0)

    result = []
    for i in range(len(daily_dates)):
        ctl_val = ctl[i]
        atl_val = atl[i]
        tsb_val = ctl_val - atl_val
        fb_val = ctl_val / atl_val if atl_val > 0 else 0.0

        result.append(
            {
                "date": daily_dates[i],
                "ctl": ctl_val,
                "atl": atl_val,
                "tsb": tsb_val,
                "fb": fb_val,
            }
        )

    return result


def training_load_to_dict(result: TrainingLoadResult) -> dict[str, Any]:
    """Serialize TrainingLoadResult to a plain dict."""
    return {
        "date": result.date,
        "ctl": result.ctl,
        "atl": result.atl,
        "tsb": result.tsb,
        "fitness_fatigue": result.fitness_fatigue,
    }