"""
3D Impulse-Response Model (Kontro et al. 2026).

Decomposes training load into three parallel Banister models, one per
energy system: aerobic (CP), glycolytic (W'), alactic (Pmax).

Each system has its own fitness (slow decay) and fatigue (fast decay)
dynamics, allowing specific adaptation tracking.

Based on:
- Kontro et al. 2026 (PLOS One): 3D IR model
- Banister et al. 1975/1990: Original impulse-response model
"""

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# Empirical parameters from Kontro et al. 2026 (Table 2)
# These are population priors — individual fitting improves accuracy.
# Normalized parameters for our Strain Score implementation.
# SS values are ~100-500 per activity; these k1/k2 values produce
# performance in the same units as the input (watts for CP, joules for W').
SYSTEM_PARAMETERS = {
    "cp": {
        "tau_fitness": 52.0,
        "tau_fatigue": 10.0,
        "k1": 0.001,
        "k2": 0.0005,
    },
    "wp": {
        "tau_fitness": 5.0,
        "tau_fatigue": 5.0,
        "k1": 0.1,
        "k2": 0.08,
    },
    "pmax": {
        "tau_fitness": 10.0,
        "tau_fatigue": 4.0,
        "k1": 0.05,
        "k2": 0.03,
    },
}


@dataclass
class SystemState:
    """State of a single energy system."""
    fitness: float = 0.0      # g(t) — fitness component
    fatigue: float = 0.0     # h(t) — fatigue component
    performance: float = 0.0 # p(t) = k1*g - k2*h


@dataclass
class ThreeDIMResult:
    """3D IR model result."""
    cp_state: SystemState = field(default_factory=SystemState)
    wp_state: SystemState = field(default_factory=SystemState)
    pmax_state: SystemState = field(default_factory=SystemState)

    # Predicted fitness parameters
    predicted_cp: float = 0.0
    predicted_wp: float = 0.0
    predicted_pmax: float = 0.0

    # Fitness trends (1-day change)
    cp_trend: float = 0.0
    wp_trend: float = 0.0
    pmax_trend: float = 0.0


class ThreeDIMModel:
    """
    Three-dimensional impulse-response model.

    Tracks fitness and fatigue for three energy systems independently,
    then combines them for performance prediction.
    """

    def __init__(self, params: dict | None = None):
        self.params = params or SYSTEM_PARAMETERS
        self.cp = SystemState()
        self.wp = SystemState()
        self.pmax = SystemState()

        # Previous day states for trend calculation
        self._prev_cp = SystemState()
        self._prev_wp = SystemState()
        self._prev_pmax = SystemState()

        # Last update date
        self._last_date: date | None = None

    def _decay(self, value: float, tau: float, days: float) -> float:
        """Exponential decay: value * exp(-days / tau)."""
        return value * np.exp(-days / tau)

    def _update_system(
        self,
        state: SystemState,
        params: dict,
        strain: float,
        days: float = 1.0,
    ) -> SystemState:
        """
        Update a single energy system.

        Args:
            state: Current system state.
            params: System parameters (tau_fitness, tau_fatigue, k1, k2).
            strain: Training strain for this system (SS_CP, SS_W', or SS_Pmax).
            days: Days since last update.

        Returns:
            Updated system state.
        """
        # Decay existing fitness and fatigue
        new_fitness = self._decay(state.fitness, params["tau_fitness"], days)
        new_fatigue = self._decay(state.fatigue, params["tau_fatigue"], days)

        # Add new strain
        new_fitness += params["k1"] * strain
        new_fatigue += params["k2"] * strain

        # Compute performance
        performance = params["k1"] * new_fitness - params["k2"] * new_fatigue

        return SystemState(
            fitness=new_fitness,
            fatigue=new_fatigue,
            performance=performance,
        )

    def update(
        self,
        ss_cp: float,
        ss_wp: float,
        ss_pmax: float,
        current_date: date | None = None,
        last_date: date | None = None,
    ) -> ThreeDIMResult:
        """
        Update all three systems with new strain data.

        Args:
            ss_cp: Aerobic strain score.
            ss_wp: Glycolytic strain score.
            ss_pmax: Alactic strain score.
            current_date: Date of this training session.
            last_date: Date of last update (for decay calculation).

        Returns:
            ThreeDIMResult with updated states and predictions.
        """
        # Calculate days between updates
        if current_date and last_date:
            days = (current_date - last_date).days or 1
        else:
            days = 1

        # Save previous states for trend calculation
        self._prev_cp = SystemState(**vars(self.cp))
        self._prev_wp = SystemState(**vars(self.wp))
        self._prev_pmax = SystemState(**vars(self.pmax))

        # Update each system
        self.cp = self._update_system(self.cp, self.params["cp"], ss_cp, days)
        self.wp = self._update_system(self.wp, self.params["wp"], ss_wp, days)
        self.pmax = self._update_system(self.pmax, self.params["pmax"], ss_pmax, days)

        # Compute trends (1-day change in performance)
        cp_trend = self.cp.performance - self._prev_cp.performance
        wp_trend = self.wp.performance - self._prev_wp.performance
        pmax_trend = self.pmax.performance - self._prev_pmax.performance

        self._last_date = current_date

        return ThreeDIMResult(
            cp_state=self.cp,
            wp_state=self.wp,
            pmax_state=self.pmax,
            predicted_cp=self.cp.performance,
            predicted_wp=self.wp.performance,
            predicted_pmax=self.pmax.performance,
            cp_trend=round(cp_trend, 2),
            wp_trend=round(wp_trend, 2),
            pmax_trend=round(pmax_trend, 2),
        )

    def decay(self, days: int) -> ThreeDIMResult:
        """
        Decay all systems without new training input.

        Used for predicting fitness on rest days or between training sessions.
        """
        self._prev_cp = SystemState(**vars(self.cp))
        self._prev_wp = SystemState(**vars(self.wp))
        self._prev_pmax = SystemState(**vars(self.pmax))

        self.cp = self._update_system(self.cp, self.params["cp"], 0.0, days)
        self.wp = self._update_system(self.wp, self.params["wp"], 0.0, days)
        self.pmax = self._update_system(self.pmax, self.params["pmax"], 0.0, days)

        return ThreeDIMResult(
            cp_state=self.cp,
            wp_state=self.wp,
            pmax_state=self.pmax,
            predicted_cp=self.cp.performance,
            predicted_wp=self.wp.performance,
            predicted_pmax=self.pmax.performance,
            cp_trend=0.0,
            wp_trend=0.0,
            pmax_trend=0.0,
        )

    def get_readiness_from_fitness(self) -> float:
        """
        Compute a fitness-based readiness score (0-100).

        Based on the balance of fitness vs fatigue across all systems.
        Higher fitness and lower fatigue = better readiness.
        """
        # Normalize each system's fitness/fatigue ratio
        cp_ratio = self.cp.fitness / (self.cp.fatigue + 1e-10)
        wp_ratio = self.wp.fitness / (self.wp.fatigue + 1e-10)
        pmax_ratio = self.pmax.fitness / (self.pmax.fatigue + 1e-10)

        # Weight by system importance (aerobic is most important for endurance)
        combined = cp_ratio * 0.5 + wp_ratio * 0.3 + pmax_ratio * 0.2

        # Map to 0-100 scale (logistic transform)
        score = 100.0 / (1.0 + np.exp(-(combined - 1.0) * 2))
        return float(np.clip(score, 0, 100))

    def to_dict(self) -> dict[str, Any]:
        """Serialize current model state to a plain dict."""
        return {
            "cp": {
                "fitness": self.cp.fitness,
                "fatigue": self.cp.fatigue,
                "performance": self.cp.performance,
            },
            "wp": {
                "fitness": self.wp.fitness,
                "fatigue": self.wp.fatigue,
                "performance": self.wp.performance,
            },
            "pmax": {
                "fitness": self.pmax.fitness,
                "fatigue": self.pmax.fatigue,
                "performance": self.pmax.performance,
            },
            "readiness_from_fitness": self.get_readiness_from_fitness(),
        }


def three_dim_to_dict(result: ThreeDIMResult) -> dict[str, Any]:
    """Serialize ThreeDIMResult to a plain dict."""
    return {
        "cp": {
            "fitness": result.cp_state.fitness,
            "fatigue": result.cp_state.fatigue,
            "performance": result.cp_state.performance,
        },
        "wp": {
            "fitness": result.wp_state.fitness,
            "fatigue": result.wp_state.fatigue,
            "performance": result.wp_state.performance,
        },
        "pmax": {
            "fitness": result.pmax_state.fitness,
            "fatigue": result.pmax_state.fatigue,
            "performance": result.pmax_state.performance,
        },
        "predicted_cp": result.predicted_cp,
        "predicted_wp": result.predicted_wp,
        "predicted_pmax": result.predicted_pmax,
        "cp_trend": result.cp_trend,
        "wp_trend": result.wp_trend,
        "pmax_trend": result.pmax_trend,
    }