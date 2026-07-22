"""
Shared constants for the Personal Cycling Agent.

This module centralizes numeric constants used across multiple files.
Scientific model parameters (tau values, k coefficients, etc.) remain
in their respective analytics modules where they belong alongside
the formulas they parameterize.
"""

# ---------------------------------------------------------------------------
# Training Load — CTL / ATL half-lives (days)
# Used in: training_load.py, feature_engineering.py, weekly_planner.py
# ---------------------------------------------------------------------------
CTL_HALFLIFE_DAYS = 18.0
ATL_HALFLIFE_DAYS = 7.0

# ---------------------------------------------------------------------------
# Sync Limits
# Used in: garmin_connect.py, visualize.py
# ---------------------------------------------------------------------------
MAX_SYNC_DAYS = 3650  # 10-year cap on unbounded sync
DEFAULT_CLI_SYNC_DAYS = 90

# ---------------------------------------------------------------------------
# Garmin API Rate Limiting
# Used in: garmin_connect.py
# ---------------------------------------------------------------------------
GARMIN_RATE_LIMIT_MIN_INTERVAL = 1.0  # seconds
GARMIN_RATE_LIMIT_MAX_BACKOFF = 300.0  # seconds
GARMIN_RATE_LIMIT_BACKOFF_FACTOR = 2.0
GARMIN_WELLNESS_POLL_INTERVAL = 0.5  # seconds between wellness API calls
GARMIN_FIT_DOWNLOAD_INTERVAL = 1.0  # seconds between FIT downloads
GARMIN_ACTIVITY_BATCH_SIZE = 100

# ---------------------------------------------------------------------------
# Power Metrics
# Used in: power_metrics.py, strain_score.py
# ---------------------------------------------------------------------------
NP_POWER_EXPONENT = 4
NP_FOURTH_ROOT = 0.25  # 1/4
SECONDS_PER_HOUR = 3600.0
JOULES_PER_KILOJOULE = 1000.0

# ---------------------------------------------------------------------------
# Rolling CP Lookback Window (days)
# Used in: main.py
# ---------------------------------------------------------------------------
ROLLING_CP_WINDOW_DAYS = 90

# ---------------------------------------------------------------------------
# Readiness Score Thresholds
# Used in: visualize.py
# ---------------------------------------------------------------------------
READINESS_GOOD_THRESHOLD = 70
READINESS_MODERATE_THRESHOLD = 50

# ---------------------------------------------------------------------------
# CTL Interpretation Thresholds
# Used in: visualize.py
# ---------------------------------------------------------------------------
CTL_VERY_HIGH = 150
CTL_HIGH = 100
CTL_MODERATE = 50

# ---------------------------------------------------------------------------
# TSB (Training Stress Balance) Thresholds
# Used in: visualize.py
# ---------------------------------------------------------------------------
TSB_FRESH = 10
TSB_NEUTRAL_FLOOR = -10
TSB_TIRED = -10

# ---------------------------------------------------------------------------
# DFA-a1 Lactate Threshold Targets
# Used in: threshold.py
# ---------------------------------------------------------------------------
DFA_LT1_TARGET = 0.75  # First lactate threshold
DFA_LT2_TARGET = 0.50  # Second lactate threshold / critical power
DFA_ZONE2_VIOLATION_THRESHOLD = 0.75
DFA_ZONE2_AUDIT_PASS_THRESHOLD = 0.10  # Max 10% violation rate

# ---------------------------------------------------------------------------
# Durability — Fatigue State Thresholds (kJ)
# Used in: durability.py
# ---------------------------------------------------------------------------
FATIGUED_KJ = 1000
DEEPLY_FATIGUED_KJ = 1500

# ---------------------------------------------------------------------------
# Weather / Rideability Thresholds
# Used in: weekly_planner.py, services/weather.py
# ---------------------------------------------------------------------------
WEATHER_PRECIP_INDOOR_THRESHOLD = 60  # percent chance
WEATHER_PRECIP_BACKUP_THRESHOLD = 30  # percent chance
WEATHER_WIND_INDOOR_THRESHOLD = 30  # km/h
WEATHER_TEMP_MAX = 35  # °C heat warning
WEATHER_TEMP_MIN = -5  # °C cold warning
WEATHER_PRECIP_RIDEABLE = 40  # percent chance

# ---------------------------------------------------------------------------
# Default Profile Values
# Used in: main.py, visualize.py
# ---------------------------------------------------------------------------
DEFAULT_MAX_HR = 190
DEFAULT_RESTING_HR = 70
DEFAULT_CTL_FALLBACK = 80.0
DEFAULT_ATL_FALLBACK = 60.0
DEFAULT_PLANNED_TSS = 80.0

# ---------------------------------------------------------------------------
# Default Analysis Windows (days)
# Used in: readiness.py, store.py, journal.py, scheduler.py
# ---------------------------------------------------------------------------
DEFAULT_ANALYSIS_WINDOW_DAYS = 30
DEFAULT_JOURNAL_LINES = 30
DEFAULT_SQL_QUERY_LIMIT = 100

# ---------------------------------------------------------------------------
# Scheduler Defaults
# Used in: tasks/scheduler.py
# ---------------------------------------------------------------------------
DEFAULT_ACTIVITY_SYNC_MINUTES = 30
DEFAULT_WELLNESS_SYNC_HOURS = 6

# ---------------------------------------------------------------------------
# UI Defaults
# Used in: visualize.py, ui_helpers.py
# ---------------------------------------------------------------------------
SYNC_LOG_CAP = 500  # max sync log entries to keep in memory
DOWNSAMPLE_MAX_POINTS = 10_000
EARTH_RADIUS_KM = 6371.0
MS_TO_KMH = 3.6
MS_TO_MPH = 2.23694
MILES_TO_KM = 1.60934
KM_TO_MILES = 0.621371
M_TO_FEET = 3.28084

# ---------------------------------------------------------------------------
# W' Estimation Defaults
# Used in: w_prime.py
# ---------------------------------------------------------------------------
W_PRIME_MIN_BALANCE_THRESHOLD = 0.40  # 40%
W_PRIME_ROLLING_WINDOW_SEC = 30
W_PRIME_DEFAULT_BALANCE_PCT = 100.0

# ---------------------------------------------------------------------------
# Decoupling Defaults
# Used in: decoupling.py
# ---------------------------------------------------------------------------
DECOUPLING_DRIFT_THRESHOLD_PCT = 5.0
DECOUPLING_MIN_SAMPLES = 10

# ---------------------------------------------------------------------------
# HR Training Load — Banister TRIMP
# Used in: hr_training_load.py
# ---------------------------------------------------------------------------
BANISTER_B_MALE = 1.92
BANISTER_B_FEMALE = 1.67
BANISTER_TRIMP_SCALING = 0.64
THRESHOLD_HR_FRACTION = 0.90  # 90% of HR reserve ≈ LT
MIN_VALID_HR_SAMPLES = 60  # seconds
CALIBRATION_FACTOR_MIN = 0.5
CALIBRATION_FACTOR_MAX = 3.0
MIN_DUAL_SENSOR_RIDES = 3

# ---------------------------------------------------------------------------
# LLM / HTTP Timeouts
# Used in: llm_client.py, services/weather.py, visualize.py
# ---------------------------------------------------------------------------
HTTP_TIMEOUT_SEC = 10