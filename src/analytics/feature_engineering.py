"""
Feature engineering pipeline for ML-based recovery prediction.

Implements per-athlete centering, lag expansion, EWMA, derived features,
and correlation pruning as described in Rothschild et al. 2024.
"""

import logging
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_features(
    wellness_records: list[dict[str, Any]],
    activity_metrics: list[dict[str, Any]] | None = None,
    morning_checkins: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """
    Engineer features from raw wellness, activity, and subjective data.

    Args:
        wellness_records: List of dicts with keys: date, rmssd, resting_hr,
            stress, sleep_score, sleep_hours, steps, body_battery_end
        activity_metrics: List of dicts with keys: start_date, tss, np, ifr,
            w_prime_min_balance, decoupling_drift
        morning_checkins: List of dicts with keys: date, perceived_readiness,
            soreness, life_stress, sleep_quality, mood, energy, motivation,
            pain_score

    Returns:
        DataFrame with engineered features, indexed by date.
    """
    if not wellness_records:
        return pd.DataFrame()

    # Build base DataFrame from wellness
    df = pd.DataFrame(wellness_records)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")

    # Merge activity metrics (aggregate to daily)
    if activity_metrics:
        act_df = pd.DataFrame(activity_metrics)
        if not act_df.empty and "start_date" in act_df.columns:
            act_df["start_date"] = pd.to_datetime(act_df["start_date"])
            daily_activity = act_df.groupby(act_df["start_date"].dt.date).agg(
                tss=("tss", "sum"),
                np=("np", "max"),
                ifr=("ifr", "mean"),
                w_prime_min_balance=("w_prime_min_balance", "min"),
                decoupling_drift=("decoupling_drift", "mean"),
            ).reset_index()
            daily_activity["date"] = pd.to_datetime(daily_activity.columns[0])
            daily_activity = daily_activity.set_index("date")
            df = df.join(daily_activity, rsuffix="_activity", how="left")

    # Merge morning checkins
    if morning_checkins:
        ci_df = pd.DataFrame(morning_checkins)
        ci_df["date"] = pd.to_datetime(ci_df["date"])
        ci_df = ci_df.set_index("date")
        df = df.join(ci_df, rsuffix="_checkin", how="left")

    # Forward-fill for 1-2 days, NaN for longer gaps
    df = df.ffill(limit=2)

    # --- Per-athlete z-scores (30-day rolling baseline) ---
    for col in ["rmssd", "resting_hr"]:
        if col in df.columns:
            rolling = df[col].rolling(window=30, min_periods=7)
            mean = rolling.mean()
            std = rolling.std()
            std = std.replace(0, 1)  # avoid division by zero
            df[f"{col}_z"] = (df[col] - mean) / std

    # --- 7-day lag features ---
    for col in ["rmssd", "resting_hr", "stress", "sleep_score", "tss"]:
        if col in df.columns:
            for lag in [1, 2, 3, 7]:
                df[f"{col}_lag{lag}"] = df[col].shift(lag)

    # --- EWMA for load metrics ---
    if "tss" in df.columns:
        # CTL-like: 18-day half-life → alpha = 1 - exp(ln(0.5)/18) ≈ 0.038
        df["ctl"] = df["tss"].ewm(halflife=18, min_periods=1).mean()
        # ATL-like: 7-day half-life
        df["atl"] = df["tss"].ewm(halflife=7, min_periods=1).mean()
        # ACWR
        df["acwr"] = df["atl"] / df["ctl"].replace(0, 1)
        # 7-day rolling TSS
        df["tss_7d"] = df["tss"].rolling(window=7, min_periods=1).sum()

    # --- Derived features ---
    # Sleep index: normalized sleep quality × duration
    if "sleep_score" in df.columns and "sleep_hours" in df.columns:
        df["sleep_index"] = (df["sleep_score"] / 100.0) * (df["sleep_hours"] / 10.0)
        df["sleep_index"] = df["sleep_index"].clip(0, 1)

    # Well-being score (from subjective data)
    if "perceived_readiness" in df.columns:
        df["wb_score"] = df["perceived_readiness"] / 10.0
    if "soreness" in df.columns and "life_stress" in df.columns:
        soreness_norm = df["soreness"].fillna(3.5) / 7.0
        stress_norm = df["life_stress"].fillna(3.5) / 7.0
        df["wb_composite"] = (1 - soreness_norm) * (1 - stress_norm)

    # Decoupling trend (7-day rolling average)
    if "decoupling_drift" in df.columns:
        df["decoupling_trend"] = df["decoupling_drift"].rolling(
            window=7, min_periods=1
        ).mean()

    # W' balance trend
    if "w_prime_min_balance" in df.columns:
        df["w_prime_trend"] = df["w_prime_min_balance"].rolling(
            window=7, min_periods=1
        ).mean()

    # Correlation pruning: drop features with |r| > 0.85
    df = _prune_correlated_features(df, threshold=0.85)

    # Drop rows with too many NaNs (need at least 50% of features)
    n_features = len([c for c in df.columns if c.endswith(("_z", "_lag", "_trend", "ctl", "atl", "acwr", "tss_7d", "sleep_index", "wb_score", "wb_composite"))])
    if n_features > 0:
        df = df.dropna(thresh=n_features // 2)

    return df


def _prune_correlated_features(df: pd.DataFrame, threshold: float = 0.85) -> pd.DataFrame:
    """Drop features highly correlated with another feature (|r| > threshold)."""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if len(numeric_cols) < 2:
        return df

    corr_matrix = df[numeric_cols].corr().abs()
    # Upper triangle
    upper = corr_matrix.where(
        np.triu(np.ones(corr_matrix.shape), k=1).astype(bool)
    )
    to_drop = [col for col in upper.columns if any(upper[col] > threshold)]
    if to_drop:
        logger.info(f"Pruned {len(to_drop)} correlated features: {to_drop}")
        df = df.drop(columns=to_drop)
    return df


def get_feature_names() -> list[str]:
    """Return the list of feature names used by the ML model."""
    return [
        "rmssd_z",
        "resting_hr_z",
        "sleep_index",
        "wb_score",
        "wb_composite",
        "acwr",
        "ctl",
        "atl",
        "tss_7d",
        "decoupling_trend",
        "w_prime_trend",
        "stress",
        "sleep_score",
        "sleep_hours",
        "body_battery_end",
    ]