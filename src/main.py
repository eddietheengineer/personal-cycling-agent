"""
Cycling AI Agent - Main Pipeline Orchestrator.

Runs the full daily pipeline:
1. Ingest data from Garmin Connect
2. Run analytics (readiness, thresholds, W', durability, decoupling)
3. Build LLM prompt from analytics + user profile
4. Generate training prescription via local LLM
5. Publish prescription via MQTT

Usage:
    python -m src.main              # run full pipeline
    python -m src.main --ingest     # ingest only
    python -m src.main --analyze    # analytics only (from existing DB)
    python -m src.main --prescribe  # prescribe only (from existing analytics)
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_DIR = PROJECT_ROOT / ".venv"

# Use venv Python if available
_venv_python = VENV_DIR / "bin" / "python"
if _venv_python.exists() and _venv_python != Path(sys.executable):
    os.execv(str(_venv_python), [str(_venv_python), "-m", "src.main"] + sys.argv[1:])

from src import config

config.setup()

from src.ingestion.garmin_connect import sync_garmin, sync_activities
from src.db.store import CyclingDB
from src.analytics.readiness import assess_readiness, readiness_to_dict
from src.analytics.threshold import analyze_thresholds, threshold_to_dict
from src.analytics.w_prime import estimate_w_prime_from_activity, w_prime_to_dict
from src.analytics.durability import compute_durability, durability_to_dict
from src.analytics.decoupling import compute_decoupling, decoupling_to_dict
from src.agent.prompt_builder import build_system_prompt
from src.agent.llm_client import generate_with_retries
from src.agent.mqtt_publisher import publish as mqtt_publish

VAULT = config.vault_path()
DB_PATH = str(config.db_path("cycling_agent.sqlite"))
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logger = logging.getLogger("cycling_agent")


def run_ingest() -> dict:
    """Fetch and store data from Garmin Connect."""
    logger.info("Starting data ingestion...")
    counts = sync_garmin(db_path=DB_PATH)
    logger.info(f"Wellness sync complete: {counts}")

    # Sync activity streams for recent activities
    activity_counts = sync_activities(days=90, db_path=DB_PATH)
    logger.info(f"Activity sync complete: {activity_counts}")

    return {**counts, **activity_counts}


def run_analyze() -> dict:
    """Run all analytics on stored data."""
    logger.info("Starting analytics...")

    with CyclingDB(DB_PATH) as db:
        # --- Readiness ---
        wellness_records = db.get_wellness()
        if not wellness_records:
            logger.warning("No wellness data in DB; run --ingest first")
            return {}

        wellness_dicts = [dict(r) for r in wellness_records]

        # Use the latest available date
        latest_date = wellness_dicts[0].get("date", "")
        readiness_result = assess_readiness(wellness_dicts, target_date=latest_date)
        readiness_dict = readiness_to_dict(readiness_result)
        logger.info(f"Readiness: {readiness_result.state.value} - {readiness_result.recommendation}")

        # --- Recent activities for context ---
        activities = db.get_activities(oldest=(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
        activity_dicts = [dict(a) for a in activities]

        # --- Thresholds, W', Durability, Decoupling ---
        # Run analytics on recent activities that have power data
        thresholds_results = []
        w_prime_results = []
        durability_results = []
        decoupling_results = []

        for act in activity_dicts[:5]:
            activity_id = act.get("id", "")
            if not activity_id:
                continue

            # Get power stream for this activity
            power_rows = db.get_activity_streams(activity_id, "power")
            power_samples = [float(r["value"]) for r in power_rows] if power_rows else []

            # Get HR stream for decoupling
            hr_rows = db.get_activity_streams(activity_id, "heart_rate")
            hr_samples = [float(r["value"]) for r in hr_rows] if hr_rows else []

            # Get DFA-a1 stream for thresholds
            dfa_rows = db.get_activity_streams(activity_id, "dfa_a1")
            dfa_samples = [float(r["value"]) for r in dfa_rows] if dfa_rows else []

            # Thresholds (needs power + DFA-a1)
            if power_samples and dfa_samples:
                try:
                    tr = analyze_thresholds(activity_id, power_samples, dfa_samples)
                    thresholds_results.append(threshold_to_dict(tr))
                except Exception as e:
                    logger.warning(f"Threshold analysis failed for {activity_id}: {e}")

            # W' (needs power)
            if power_samples:
                try:
                    wp = estimate_w_prime_from_activity(activity_id, power_samples)
                    w_prime_results.append(w_prime_to_dict(wp))
                except Exception as e:
                    logger.warning(f"W' estimation failed for {activity_id}: {e}")

            # Durability (needs power)
            if power_samples:
                try:
                    dp = compute_durability(activity_id, power_samples)
                    durability_results.append(durability_to_dict(dp))
                except Exception as e:
                    logger.warning(f"Durability analysis failed for {activity_id}: {e}")

            # Decoupling (needs power + HR)
            if power_samples and hr_samples:
                try:
                    dc = compute_decoupling(activity_id, power_samples, hr_samples)
                    decoupling_results.append(decoupling_to_dict(dc))
                except Exception as e:
                    logger.warning(f"Decoupling analysis failed for {activity_id}: {e}")

    result = {
        "readiness": readiness_dict,
        "recent_activities": activity_dicts[:5],
        "thresholds": thresholds_results,
        "w_prime": w_prime_results,
        "durability": durability_results,
        "decoupling": decoupling_results,
    }

    # Save analytics result for the prompt builder
    result_path = VAULT / "data" / "latest_analysis.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2)

    logger.info("Analytics complete")
    return result


def run_prescribe(analysis: dict | None = None) -> str:
    """Generate a training prescription via the LLM."""
    logger.info("Building prompt and generating prescription...")

    # Load analysis if not provided
    if analysis is None:
        result_path = VAULT / "data" / "latest_analysis.json"
        if not result_path.exists():
            logger.error("No analysis found; run --analyze first")
            raise SystemExit(1)
        with open(result_path, "r") as f:
            analysis = json.load(f)

    # Build the prompt
    prompt = build_system_prompt(
        readiness=analysis.get("readiness"),
        recent_activities=analysis.get("recent_activities"),
    )

    logger.info(f"Prompt length: {len(prompt)} chars")

    # Generate prescription
    prescription = generate_with_retries(prompt)
    logger.info(f"Prescription generated: {len(prescription)} chars")

    # Save prescription
    presc_path = VAULT / "data" / "today_prescription.txt"
    with open(presc_path, "w") as f:
        f.write(prescription)

    # Publish via MQTT
    mqtt_publish(prescription, metadata=analysis.get("readiness"))

    return prescription


def main():
    parser = argparse.ArgumentParser(description="Cycling AI Agent Pipeline")
    parser.add_argument("--ingest", action="store_true", help="Run data ingestion only")
    parser.add_argument("--analyze", action="store_true", help="Run analytics only")
    parser.add_argument("--prescribe", action="store_true", help="Generate prescription only")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # If no flags, run the full pipeline
    run_all = not any([args.ingest, args.analyze, args.prescribe])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
    )

    if run_all or args.ingest:
        run_ingest()

    if run_all or args.analyze:
        analysis = run_analyze()
    else:
        analysis = None

    if run_all or args.prescribe:
        prescription = run_prescribe(analysis)
        print("\n" + "=" * 60)
        print("TODAY'S TRAINING PRESCRIPTION")
        print("=" * 60)
        print(prescription)


if __name__ == "__main__":
    main()