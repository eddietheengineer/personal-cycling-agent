"""
Individualized ML recovery model (Rothschild-style LASSO).

Implements per-athlete LASSO regression for next-day PRS prediction,
with online learning, rolling window validation, and drift detection.

Based on Rothschild et al. 2024 (Eur J Appl Physiol 124:3279):
https://link.springer.com/article/10.1007/s00421-024-05530-2
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

# Population priors (equal weights, normalized) — used during cold start
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


@dataclass
class ModelMetrics:
    """Metrics for model performance tracking."""
    n_samples: int = 0
    rmse_history: list[float] = field(default_factory=list)
    r2_history: list[float] = field(default_factory=list)
    drift_detected: bool = False
    last_trained: str = ""
    status: str = "cold_start"  # cold_start | warming | trained


@dataclass
class PredictionResult:
    """Result of a prediction."""
    predicted_prs: float  # 0-10 scale
    confidence: float  # 0-1, based on model status
    limiting_factor: str  # which feature contributed most to low score
    feature_contributions: dict[str, float]  # per-feature contribution


class IndividualRecoveryModel:
    """
    Per-athlete LASSO model for recovery prediction.

    Cold start (days 1-7): population priors, high uncertainty.
    Warm up (days 8-28): online learning with partial_fit().
    Steady state (day 29+): individual model, periodic drift detection.
    """

    def __init__(self, feature_names: list[str] | None = None):
        if feature_names is None:
            from src.analytics.feature_engineering import get_feature_names
            feature_names = get_feature_names()

        self.feature_names = feature_names
        self.scaler = StandardScaler()
        self.model = SGDRegressor(
            penalty="l1",
            loss="squared_error",
            learning_rate="constant",
            eta0=0.01,
            tol=1e-4,
            max_iter=1000,
        )
        self.metrics = ModelMetrics()
        self._ewma_residual = None
        self._ewma_span = 14

    def train(self, features: pd.DataFrame, targets: pd.Series) -> dict[str, Any]:
        """Full training on historical data."""
        if features.empty or targets.empty:
            return {"error": "No data for training"}

        # Align features and targets
        common_idx = features.index.intersection(targets.index)
        X = features.loc[common_idx, features.columns.intersection(self.feature_names)]
        y = targets.loc[common_idx]

        # Fill NaN with 0 (neutral) instead of dropping — handles sparse historical data
        X = X.fillna(0)
        mask = y.notna()
        X = X[mask]
        y = y[mask]

        if len(X) < 7:
            return {"error": "Insufficient data (need ≥7 samples)"}

        # Reset scaler for full retrain (loaded models may have stale state)
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

        # Determine status
        if self.metrics.n_samples < 7:
            self.metrics.status = "cold_start"
        elif self.metrics.n_samples < 28:
            self.metrics.status = "warming"
        else:
            self.metrics.status = "trained"

        logger.info(
            f"Model trained: n={self.metrics.n_samples}, RMSE={rmse:.2f}, "
            f"R²={r2:.3f}, status={self.metrics.status}"
        )

        return {
            "n_samples": self.metrics.n_samples,
            "rmse": rmse,
            "r2": r2,
            "status": self.metrics.status,
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
            self.metrics.status = "trained"

        return {
            "n_samples": self.metrics.n_samples,
            "status": self.metrics.status,
        }

    def predict(self, features: pd.DataFrame) -> PredictionResult:
        """Predict next-day PRS from current features."""
        X = features.loc[:, features.columns.intersection(self.feature_names)]

        if X.empty:
            return PredictionResult(
                predicted_prs=5.0,
                confidence=0.0,
                limiting_factor="no_data",
                feature_contributions={},
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
        else:
            predicted = 5.0
            contributions = {}
            limiting = "cold_start"

        # Compute confidence based on model status
        if self.metrics.status == "cold_start":
            confidence = 0.3
        elif self.metrics.status == "warming":
            confidence = min(0.3 + 0.05 * self.metrics.n_samples, 0.7)
        else:
            confidence = min(0.7 + 0.01 * (self.metrics.n_samples - 28), 0.95)

        return PredictionResult(
            predicted_prs=predicted,
            confidence=confidence,
            limiting_factor=limiting,
            feature_contributions=contributions,
        )

    def evaluate(
        self, features: pd.DataFrame, targets: pd.Series
    ) -> dict[str, float]:
        """Evaluate model on test data."""
        common_idx = features.index.intersection(targets.index)
        X = features.loc[common_idx, features.columns.intersection(self.feature_names)]
        y = targets.loc[common_idx]

        mask = X.notna().all(axis=1) & y.notna()
        X = X[mask].fillna(0)
        y = y[mask]

        if len(X) < 2:
            return {"rmse": float("nan"), "r2": float("nan")}

        try:
            X_scaled = self.scaler.transform(X)
        except ValueError:
            X_scaled = self.scaler.fit_transform(X)

        y_pred = self.model.predict(X_scaled)
        rmse = float(np.sqrt(mean_squared_error(y.values, y_pred)))
        r2 = float(r2_score(y.values, y_pred))

        return {"rmse": rmse, "r2": r2}

    def rolling_validation(
        self, features: pd.DataFrame, targets: pd.Series, window: int = 28, step: int = 7
    ) -> list[dict[str, Any]]:
        """
        Rolling window cross-validation.

        Returns list of {train_rmse, test_rmse, test_r2, window_start, window_end}.
        """
        results = []
        dates = features.index.sort_values()
        n = len(dates)

        for i in range(0, n - window, step):
            train_end = dates[i + window]
            test_end = dates[min(i + window + step, n - 1)]

            train_mask = features.index < train_end
            test_mask = (features.index >= train_end) & (features.index <= dates[test_end])

            train_X = features[train_mask]
            train_y = targets[train_mask]
            test_X = features[test_mask]
            test_y = targets[test_mask]

            if len(train_X) < 7 or len(test_X) < 2:
                continue

            # Train on window
            temp_model = IndividualRecoveryModel(self.feature_names)
            temp_model.train(train_X, train_y)

            # Evaluate on test
            eval_result = temp_model.evaluate(test_X, test_y)
            eval_result["window_start"] = str(train_end.date())
            eval_result["window_end"] = str(dates[test_end].date())
            results.append(eval_result)

        return results

    def check_drift(self, residual: float) -> bool:
        """
        EWMA-based drift detection.

        Returns True if drift detected (residual significantly different from recent average).
        """
        if self._ewma_residual is None:
            self._ewma_residual = residual
            return False

        alpha = 2 / (self._ewma_span + 1)
        self._ewma_residual = alpha * residual + (1 - alpha) * self._ewma_residual

        # Flag if residual deviates > 2 SD from EWMA (simplified)
        if abs(residual - self._ewma_residual) > 2.0:
            self.metrics.drift_detected = True
            logger.warning(f"Drift detected: residual={residual:.2f}, EWMA={self._ewma_residual:.2f}")
            return True

        return False

    def get_feature_importance(self) -> dict[str, float]:
        """Return absolute feature weights (importance)."""
        if not hasattr(self.model, "coef_") or self.model.coef_ is None:
            return {name: 0.0 for name in self.feature_names}
        importance = {}
        coef = self.model.coef_
        for i, name in enumerate(self.feature_names):
            if i < len(coef):
                importance[name] = abs(float(coef[i]))
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
            },
            "model_coef": self.model.coef_.tolist() if hasattr(self.model, 'coef_') and self.model.coef_ is not None else [],
            "model_intercept": float(np.asarray(self.model.intercept_).flatten()[0]) if hasattr(self.model, 'intercept_') and self.model.intercept_ is not None else 0.0,
            "scaler_mean": self.scaler.mean_.tolist() if hasattr(self.scaler, "mean_") and self.scaler.mean_ is not None else [],
            "scaler_scale": self.scaler.scale_.tolist() if hasattr(self.scaler, "scale_") and self.scaler.scale_ is not None else [],
        }
        path.write_text(json.dumps(data, indent=2))
        logger.info(f"Model saved to {path}")

    def load(self, path: str | Path) -> bool:
        """Load model from JSON."""
        path = Path(path)
        if not path.exists():
            return False

        data = json.loads(path.read_text())
        self.feature_names = data["feature_names"]
        self.metrics = ModelMetrics(**data["metrics"])
        coef_data = data.get("model_coef", [])
        if coef_data:
            self.model.coef_ = np.array(coef_data)
        intercept_data = data.get("model_intercept", 0.0)
        if intercept_data is not None:
            self.model.intercept_ = np.array([intercept_data])
        if data.get("scaler_mean"):
            self.scaler.mean_ = np.array(data["scaler_mean"])
            self.scaler.scale_ = np.array(data["scaler_scale"])
            self.scaler.n_samples_seen_ = np.array([self.metrics.n_samples])

        logger.info(f"Model loaded from {path}: status={self.metrics.status}")
        return True