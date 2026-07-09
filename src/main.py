"""
Cycling AI Agent - Main Pipeline Orchestrator.

Runs the full daily pipeline:
1. Ingest data from Intervals.icu
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
from datetime import datetime, timedelta

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src import config

config.setup()

from src.ingestion.intervals_api import fetch_wellness, fetch_activities
from src.db.store import CyclingDB
from src.analytics.readiness import assess_readiness, readiness_to_dict
from src.analytics.threshold import threshold_to_dict
from src.analytics.w_prime import w_prime_to_dict
from src.analytics.durability import durability_to_dict
from src.analytics.decoupling import decoupling_to_dict
from src.agent.prompt_builder import build_system_prompt
from src.agent.llm_client import generate_with_retries
from src.agent.mqtt_publisher import publish as mqtt_publish

VAULT = config.vault_path()
DB_PATH = str(config.db_path("cycling_agent.sqlite"))
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logger = logging.getLogger("cycling_agent")


def run_ingest() -> dict:
    """Fetch and store data from Intervals.icu."""
    from src.ingestion.intervals_api import ingest_all

    logger.info("Starting data ingestion...")
    counts = ingest_all(db_path=DB_PATH)
    logger.info(f"Ingestion complete: {counts}")
    return counts


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
        today = datetime.now().strftime("%Y-%m-%d")

        # Use the latest available date if today has no data
        latest_date = wellness_dicts[0]["id"]
        readiness_result = assess_readiness(wellness_dicts, target_date=latest_date)
        readiness_dict = readiness_to_dict(readiness_result)
        logger.info(f"Readiness: {readiness_result.state.value} - {readiness_result.recommendation}")

        # --- Recent activities for context ---
        activities = db.get_activities(oldest=(datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"))
        activity_dicts = [dict(a) for a in activities]

    result = {
        "readiness": readiness_dict,
        "recent_activities": activity_dicts[:5],
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