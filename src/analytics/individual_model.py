"""
Individual model fitting (Rothschild approach).

Uses online SGD/LASSO to converge to personal recovery weights after ~28 days,
outperforming group models.

Based on:
- Rothschild et al. 2024 (Eur J Appl Physiol 124:3279):
  Individual models vary greatly (5x RMSE range). Key variables differ per person.
  Group models fail for individuals. Online SGD/LASSO converges to personal weights.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import SGDRegressor
from sklearn.metrics import mean_squared_error, r2_score
from sklearn.preprocessing import StandardScaler

logger = logging.getLogger(__name__)


@dataclass
class IndividualMetrics:
    """Metrics for individual model performance."""
    n_samples: int = 0
    rmse_history: list[float] = field(default_factory=list)
    r2_history: list[float] = field(default_factory=list)
    drift_detected: bool = False
    last_trained: str = ""
    status: str = "cold_start"  # cold_start | warming | converged
    convergence_day: int = 0  # Day when model converged (typically ~28)


@dataclass
class IndividualPrediction:
    """Result of an individual model prediction."""
    predicted_prs: float  # Predicted physiological readiness score (0-10)
    confidence: float  # 0-1 confidence score
    limiting_factor: str  # Which metric is dragging readiness down
    feature_contributions: dict[str, float]  # Per-feature contribution
    individual_weights: dict[str, float]  # Personalized feature weights


class IndividualizedModel:
    """
    Rothschild-style individualized recovery model.

    Starts with population priors, then uses online learning to converge
    to personal weights. After ~28 days of data, individual models
    outperform group models by significant margins.

    Key insight from Rothschild 2024:
    - Individual models vary greatly (5x RMSE range across athletes)
    - Key variables differ per person (some are HRV-driven, others stress-driven)
    - Group models fail for individuals
    - Online SGD/LASSO converges to personal weights after ~28 days
    """

    # Population priors (equal weights, normalized) — starting point before personalization
    POPULATION_PRIORS = {
        "rmssd_z": 0.15,
        "resting_hr_z": 0.15,
        "sleep_index": 0.15,
        "wb_score": 0.15,
        "wb_composite": 0.10,
        "acwr": 0.05,
        "ctl": 0.05,
        "atl": 0.05,
        "tss_7d": 0.05,
        "decoupling_trend": 0.05,
        "w_prime_trend": 0.05,
    }

    def __init__(self, feature_names: list[str] | None = None):
        self.feature_names = feature_names or list(self.POPULATION_PRIORS.keys())
        self.scaler = StandardScaler()
        self.model = SGDRegressor(
            loss="squared_error",
            penalty="l1",
            alpha=0.01,  # L1 regularization strength
            learning_rate="adaptive",
            eta0=0.01,
        )
        self.metrics = IndividualMetrics()

        # Convergence tracking
        self._convergence_threshold = 0.05  # RMSE change < 5% for 7 consecutive days
        self._stable_rmse_count = 0
        self._prev_rmse = float("inf")

    def train(self, features: pd.DataFrame, targets: pd.Series) -> dict[str, Any]:
        """Full training on historical data."""
        if features.empty or targets.empty:
            return {"error": "No data"}

        # Align features and targets
        common_idx = features.index.intersection(targets.index)
        X = features.loc[common_idx, features.columns.intersection(self.feature_names)]
        y = targets.loc[common_idx]

        # Fill NaN with 0 (handles sparse historical data)
        X = X.fillna(0)
        mask = y.notna()
        X = X[mask]
        y = y[mask]

        if len(X) < 7:
            return {"error": "Insufficient data (need ≥7 samples)"}

        # Reset scaler for full retrain
        self.scaler = StandardScaler()
        X_scaled = self.scaler.fit_transform(X)

        # Train model
        self.model.fit(X_scaled, y.values)

        # Evaluate
        y_pred = self.model.predict(X_scaled)
        rmse = float(np.sqrt(mean_squared_error(y.values, y_pred)))
        r2 = float(r2_score(y.values, y_pred))

        # Update metrics
        self.metrics.n_samples = len(X)
        self.metrics.rmse_history.append(rmse)
        self.metrics.r2_history.append(r2)
        self.metrics.last_trained = str(pd.Timestamp.now().date())

        # Check convergence
        if len(self.metrics.rmse_history) >= 2:
            rmse_change = abs(self.metrics.rmse_history[-1] - self.metrics.rmse_history[-2])
            if rmse_change < self._convergence_threshold * self._prev_rmse:
                self._stable_rmse_count += 1
            else:
                self._stable_rmse_count = 0

            if self._stable_rmse_count >= 7:
                self.metrics.convergence_day = self.metrics.n_samples
                self.metrics.status = "converged"
            elif self.metrics.n_samples >= 28:
                self.metrics.status = "warming"
            else:
                self.metrics.status = "cold_start"

            self._prev_rmse = rmse

        # Drift detection: if RMSE increases by >50% from baseline
        if len(self.metrics.rmse_history) >= 3:
            baseline = np.mean(self.metrics.rmse_history[:3])
            if self.metrics.rmse_history[-1] > baseline * 1.5:
                self.metrics.drift_detected = True

        logger.info(
            f"Individual model trained: n={self.metrics.n_samples}, "
            f"RMSE={rmse:.2f}, R²={r2:.3f}, status={self.metrics.status}"
        )

        return {
            "n_samples": self.metrics.n_samples,
            "rmse": rmse,
            "r2": r2,
            "status": self.metrics.status,
            "convergence_day": self.metrics.convergence_day,
        }

    def partial_fit(self, features: pd.DataFrame, target: float) -> dict[str, Any]:
        """Online learning with a single new data point."""
        if features.empty:
            return {"error": "No features"}

        X = features.loc[:, features.columns.intersection(self.feature_names)]
        if X.empty or X.isna().any().any():
            return {"error": "Missing features"}

        # Scale (using existing scaler or fit if cold start)
        if self.metrics.n_samples == 0:
            X_scaled = self.scaler.fit_transform(X)
        else:
            X_scaled = self.scaler.transform(X)

        # Update model
        self.model.partial_fit(X_scaled, np.array([target]))
        self.metrics.n_samples += 1

        # Update status
        if self.metrics.n_samples < 7:
            self.metrics.status = "cold_start"
        elif self.metrics.n_samples < 28:
            self.metrics.status = "warming"
        else:
            self.metrics.status = "converged"

        return {
            "n_samples": self.metrics.n_samples,
            "status": self.metrics.status,
        }

    def predict(self, features: pd.DataFrame) -> IndividualPrediction:
        """Predict next-day PRS from current features."""
        X = features.loc[:, features.columns.intersection(self.feature_names)]

        if X.empty:
            return IndividualPrediction(
                predicted_prs=5.0,
                confidence=0.0,
                limiting_factor="no_data",
                feature_contributions={},
                individual_weights={},
            )

        # Handle missing features: fill with 0 (neutral)
        X_filled = X.fillna(0)

        # Scale - handle cold start where scaler isn't fitted
        if hasattr(self.scaler, "mean_") and self.scaler.mean_ is not None:
            X_scaled = self.scaler.transform(X_filled)
        else:
            X_scaled = self.scaler.fit_transform(X_filled)

        # Predict - handle cold start where model isn't trained
        if hasattr(self.model, 'coef_') and self.model.coef_ is not None:
            predicted = float(self.model.predict(X_scaled)[0])
            predicted = np.clip(predicted, 0, 10)

            # Feature contributions
            contributions = {}
            coef = self.model.coef_
            for i, name in enumerate(X_filled.columns):
                if i < len(coef):
                    contributions[name] = float(coef[i] * X_scaled[0, i])

            # Find limiting factor (most negative contribution)
            if contributions:
                limiting = min(contributions, key=contributions.get)
            else:
                limiting = "unknown"

            # Individual weights (absolute values, normalized)
            weights = {}
            total = sum(abs(c) for c in contributions.values())
            if total > 0:
                for name, contrib in contributions.items():
                    weights[name] = abs(contrib) / total
        else:
            predicted = 5.0
            contributions = {}
            limiting = "cold_start"
            weights = dict(self.POPULATION_PRIORS)

        # Confidence based on status
        if self.metrics.status == "cold_start":
            confidence = 0.3
        elif self.metrics.status == "warming":
            confidence = min(0.3 + 0.05 * self.metrics.n_samples, 0.7)
        else:
            confidence = min(0.7 + 0.01 * (self.metrics.n_samples - 28), 0.95)

        return IndividualPrediction(
            predicted_prs=predicted,
            confidence=confidence,
            limiting_factor=limiting,
            feature_contributions=contributions,
            individual_weights=weights,
        )

    def get_feature_importance(self) -> dict[str, float]:
        """Return absolute feature weights (importance)."""
        if not hasattr(self.model, 'coef_') or self.model.coef_ is None:
            return dict(self.POPULATION_PRIORS)

        importance = {}
        for i, name in enumerate(self.feature_names):
            if i < len(self.model.coef_):
                importance[name] = abs(float(self.model.coef_[i]))
        return importance

    def save(self, path: str | Path) -> None:
        """Save model to JSON."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "feature_names": self.feature_names,
            "metrics": {
                "n_samples": self.metrics.n_samples,
                "rmse_history": self.metrics.rmse_history,
                "r2_history": self.metrics.r2_history,
                "drift_detected": self.metrics.drift_detected,
                "last_trained": self.metrics.last_trained,
                "status": self.metrics.status,
                "convergence_day": self.metrics.convergence_day,
            },
            "model_coef": self.model.coef_.tolist() if hasattr(self.model, 'coef_') and self.model.coef_ is not None else [],
            "model_intercept": float(np.asarray(self.model.intercept_).flatten()[0]) if hasattr(self.model, 'intercept_') and self.model.intercept_ is not None else 0.0,
            "scaler_mean": self.scaler.mean_.tolist() if hasattr(self.scaler, "mean_") and self.scaler.mean_ is not None else [],
            "scaler_scale": self.scaler.scale_.tolist() if hasattr(self.scaler, "scale_") and self.scaler.scale_ is not None else [],
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info(f"Individual model saved to {path}")

    def load(self, path: str | Path) -> bool:
        """Load model from JSON."""
        path = Path(path)
        if not path.exists():
            return False

        data = json.loads(path.read_text())
        self.feature_names = data["feature_names"]
        self.metrics = IndividualMetrics(**data["metrics"])

        coef_data = data.get("model_coef", [])
        if coef_data:
            self.model.coef_ = np.array(coef_data)

        intercept_data = data.get("model_intercept", 0.0)
        if intercept_data:
            self.model.intercept_ = np.array([intercept_data])

        if data.get("scaler_mean"):
            self.scaler.mean_ = np.array(data["scaler_mean"])
            self.scaler.scale_ = np.array(data["scaler_scale"])
            self.scaler.n_samples_seen_ = np.array([self.metrics.n_samples])

        logger.info(f"Individual model loaded from {path}: status={self.metrics.status}")
        return True