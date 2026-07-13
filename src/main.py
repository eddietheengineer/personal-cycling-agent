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
from datetime import datetime, timedelta, date
import numpy as np

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENV_DIR = PROJECT_ROOT / ".venv"

# Use venv Python if available (guard against infinite re-exec loop)
_venv_python = VENV_DIR / "bin" / "python"
if _venv_python.exists() and _venv_python != Path(sys.executable):
    if not os.environ.get("_CYCLING_REEXEC"):
        os.environ["_CYCLING_REEXEC"] = "1"
        os.execv(str(_venv_python), [str(_venv_python), "-m", "src.main"] + sys.argv[1:])

from src import config

config.setup()

from src.ingestion.garmin_connect import sync_garmin, sync_activities
from src.ingestion.garmin_export import sync_routes_from_fit
from src.db.store import CyclingDB
from src.analytics.readiness import assess_readiness, readiness_to_dict
from src.analytics.threshold import analyze_thresholds, threshold_to_dict
from src.analytics.w_prime import estimate_w_prime_from_activity, w_prime_to_dict
from src.analytics.durability import compute_durability, durability_to_dict
from src.analytics.decoupling import compute_decoupling, decoupling_to_dict
from src.analytics.power_metrics import (
    compute_power_metrics, estimate_critical_power, power_metrics_to_dict
)
from src.analytics.training_load import (
    compute_training_load, compute_training_load_history, training_load_to_dict
)
from src.analytics.strain_score import estimate_pmax, compute_strain_score, pmax_to_dict, strain_score_to_dict
from src.analytics.three_dim_ir import ThreeDIMModel, three_dim_to_dict
from src.analytics.feedback_loop import analyze_post_ride_feedback, feedback_to_dict
from src.analytics.individual_model import IndividualizedModel
from src.analytics.feature_engineering import compute_features
from src.analytics.recovery_model import IndividualRecoveryModel
from src.analytics.prescription_engine import (
    PrescriptionInput,
    generate_prescription,
)
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
    activity_counts = sync_activities(days=1, db_path=DB_PATH)
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
        latest_date = wellness_dicts[0].get("date", "")
        # Fetch activity metrics for load-aware readiness
        metrics_rows = db.conn.execute('SELECT * FROM activity_metrics').fetchall()
        activity_metrics_dicts = [dict(r) for r in metrics_rows]
        readiness_result = assess_readiness(wellness_dicts, activity_metrics_dicts, target_date=latest_date)
        readiness_dict = readiness_to_dict(readiness_result)
        logger.info(f"Readiness: {readiness_result.state.value} - {readiness_result.recommendation}")

        # --- All activities, sorted chronologically ---
        activities = db.get_activities()
        activity_dicts = sorted(
            [dict(a) for a in activities],
            key=lambda a: a.get("start_date", ""),
        )

        # --- Walk chronologically, with CP decay over time ---
        # CP decays exponentially between activities (half-life ~28 days,
        # consistent with VO2max/CP detraining literature: ~10-15% loss
        # per week of complete detraining). Recent high-intensity data
        # can raise it again.
        _CP_HALF_LIFE_DAYS = 28.0
        _CP_DECAY_FACTOR = 1.0 - (1.0 / (2.0 ** (1.0 / _CP_HALF_LIFE_DAYS)))
        # Equivalent EWMA: CP_today = CP_yesterday + (new_cp - CP_yesterday) * alpha
        # where alpha = 1 - 0.5^(1/28) ≈ 0.0247 per day

        current_ftp = 0.0
        last_activity_date: datetime | None = None
        cp_data_points: list[dict] = []

        power_metrics_results = []
        w_prime_results = []
        durability_results = []
        decoupling_results = []
        thresholds_results = []
        tss_records = []
        strain_score_results = []
        pmax_results = []
        three_dim_model = ThreeDIMModel()
        last_three_dim_date: date | None = None

        for act in activity_dicts:
            activity_id = act.get("id", "")
            if not activity_id:
                continue

            stream_id = activity_id
            if stream_id.startswith("garmin_"):
                stream_id = stream_id[len("garmin_"):]

            power_rows = db.get_activity_streams(stream_id, "power")
            power_samples = [float(r["value"]) for r in power_rows] if power_rows else []

            hr_rows = db.get_activity_streams(stream_id, "heart_rate")
            hr_samples = [float(r["value"]) for r in hr_rows] if hr_rows else []

            dfa_rows = db.get_activity_streams(stream_id, "dfa_a1")
            dfa_samples = [float(r["value"]) for r in dfa_rows] if dfa_rows else []
            duration = len(power_samples) if power_samples else 0

            # Parse activity date for decay calculation
            act_date_str = act.get("start_date", "")[:10]
            try:
                act_date = datetime.strptime(act_date_str, "%Y-%m-%d")
            except ValueError:
                act_date = None

            # Decay FTP based on days since last activity
            if last_activity_date is not None and act_date is not None:
                days_gap = (act_date - last_activity_date).days
                if days_gap > 0 and current_ftp > 0:
                    # Exponential decay: FTP_new = FTP_old * 0.5^(days/half_life)
                    decay = 0.5 ** (days_gap / _CP_HALF_LIFE_DAYS)
                    current_ftp = max(current_ftp * decay, 50.0)

            # Compute power metrics first — we need PDC for CP estimation
            pm_result = None
            if power_samples:
                try:
                    pm_result = compute_power_metrics(
                        activity_id, power_samples, duration, current_ftp
                    )
                    power_metrics_results.append(power_metrics_to_dict(pm_result))
                    tss_records.append({
                        "date": act.get("start_date", "")[:10],
                        "tss": pm_result.tss,
                    })
                except Exception as e:
                    logger.warning(f"Power metrics failed for {activity_id}: {e}")

            # Add this activity's PDC efforts to CP data pool
            # PDC gives best-effort power at standard durations (3m, 5m, 8m, 20m)
            # which captures threshold capacity even from rides with short hard efforts
            current_w_prime = 0.0  # W' from CP regression, in joules
            if pm_result is not None and act_date is not None:
                if (datetime.now() - act_date).days <= 90:
                    pdc = pm_result.power_duration_curve
                    pdc_efforts = []
                    for dur_s in [180, 300, 480, 1200]:  # 3m, 5m, 8m, 20m
                        pwr = pdc.get(dur_s, 0)
                        if pwr > 0:
                            pdc_efforts.append({"duration": dur_s, "avg_power": pwr})
                    if pdc_efforts:
                        cp_data_points.append({"pdc_efforts": pdc_efforts})

                # Re-estimate CP from all activities seen so far
                new_cp, new_w_prime = estimate_critical_power(cp_data_points)

                # EWMA blend: allows FTP to adjust gradually in both directions
                # alpha = 1 - 0.5^(1/28) ≈ 0.0247 per day (same as decay rate)
                if new_cp > 0:
                    alpha = _CP_DECAY_FACTOR
                    current_ftp = current_ftp * (1 - alpha) + new_cp * alpha
                    current_w_prime = new_w_prime
                    logger.info(
                        f"FTP updated to {current_ftp:.0f}W, W'={current_w_prime:.0f}J "
                        f"(from {len(cp_data_points)} activities)"
                    )

            if act_date is not None:
                last_activity_date = act_date

            # W' (needs power) — use W' capacity from CP regression if available
            wp_result = None
            if power_samples:
                try:
                    wp_cap = current_w_prime / 1000.0 if current_w_prime > 0 else None
                    wp_result = estimate_w_prime_from_activity(
                        activity_id, power_samples,
                        cp_estimate=current_ftp,
                        w_prime_capacity=wp_cap,
                    )
                    w_prime_results.append(w_prime_to_dict(wp_result))
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
            dc_result = None
            if power_samples and hr_samples:
                try:
                    dc_result = compute_decoupling(activity_id, power_samples, hr_samples)
                    decoupling_results.append(decoupling_to_dict(dc_result))
                except Exception as e:
                    logger.warning(f"Decoupling analysis failed for {activity_id}: {e}")

            # Thresholds (needs power + DFA-a1)
            if power_samples and dfa_samples:
                try:
                    tr = analyze_thresholds(activity_id, power_samples, dfa_samples)
                    thresholds_results.append(threshold_to_dict(tr))
                except Exception as e:
                    logger.warning(f"Threshold analysis failed for {activity_id}: {e}")

            # --- Strain Score & Pmax ---
            if pm_result is not None and current_ftp > 0:
                try:
                    wp_joules = (wp_result.w_prime_capacity * 1000) if wp_result and wp_result.w_prime_capacity else current_ftp * 60
                    pmax_result = estimate_pmax(pm_result.power_duration_curve, current_ftp, wp_joules)
                    pmax_results.append(pmax_to_dict(pmax_result))

                    ss_result = compute_strain_score(
                        power_samples, duration, current_ftp, wp_joules,
                        pmax_result.pmax, current_ftp
                    )
                    strain_score_results.append(strain_score_to_dict(ss_result))

                    # Update 3D IR model
                    if act_date is not None:
                        three_dim_result = three_dim_model.update(
                            ss_result.ss_cp, ss_result.ss_wp, ss_result.ss_pmax,
                            current_date=act_date, last_date=last_three_dim_date
                        )
                        last_three_dim_date = act_date
                except Exception as e:
                    logger.warning(f"Strain score/3D IR failed for {activity_id}: {e}")

            # Store computed metrics in DB (separate from raw data)
            if pm_result is not None:
                db.store_activity_metrics(activity_id, {
                    "ftp_used": current_ftp,
                    "normalized_power": pm_result.normalized_power,
                    "intensity_factor": pm_result.intensity_factor,
                    "tss": pm_result.tss,
                    "variability_index": pm_result.variability_index,
                    "w_prime_capacity": wp_result.w_prime_capacity if wp_result else None,
                    "w_prime_min_balance": wp_result.min_balance_pct if wp_result else None,
                    "decoupling_drift": dc_result.drift_pct if dc_result else None,
                    "duration_sec": float(duration),
                })

        # --- Training Load ---
        training_load_result = None
        training_load_history = []
        if tss_records:
            try:
                tl = compute_training_load(tss_records, current_ftp)
                training_load_result = training_load_to_dict(tl)
                training_load_history = compute_training_load_history(tss_records)
            except Exception as e:
                logger.warning(f"Training load computation failed: {e}")

        # --- ML Model Training ---
        ml_result = {}
        try:
            wellness_dicts = [dict(r) for r in wellness_records]
            metrics_rows = db.conn.execute('SELECT * FROM activity_metrics').fetchall()
            activity_metrics_dicts = [dict(r) for r in metrics_rows]

            features_df = compute_features(
                wellness_dicts, activity_metrics_dicts, morning_checkins=None
            )

            model_path = VAULT / "data" / "recovery_model.json"
            model = IndividualRecoveryModel()

            if model.load(model_path):
                logger.info(f"Loaded existing model: {model.metrics.status} ({model.metrics.n_samples} samples)")
            else:
                logger.info("Starting fresh model (cold start)")

            if not features_df.empty:
                if "resting_hr" in features_df.columns:
                    targets = features_df["resting_hr"].shift(-1)
                    train_result = model.train(features_df, targets)
                    model.save(model_path)
                    ml_result = {"model": train_result, "features": len(features_df.columns)}
                    logger.info(f"ML model trained: {train_result}")
                else:
                    logger.info("No resting_hr data for ML training target")
            else:
                logger.info("No features available for ML training")

        except Exception as e:
            import traceback
            logger.warning(f"ML model training failed: {e}")
            logger.debug(traceback.format_exc())
            ml_result = {"error": str(e)}

        # --- Individualized Model (Rothschild approach) ---
        indiv_result = {}
        try:
            indiv_path = VAULT / "data" / "individual_model.json"
            indiv_model = IndividualizedModel()

            if indiv_model.load(indiv_path):
                logger.info(f"Loaded individual model: {indiv_model.metrics.status} ({indiv_model.metrics.n_samples} samples)")
            else:
                logger.info("Starting fresh individual model")

            if not features_df.empty:
                if "resting_hr" in features_df.columns:
                    targets = features_df["resting_hr"].shift(-1)
                    train_result = indiv_model.train(features_df, targets)
                    indiv_model.save(indiv_path)
                    indiv_result = {"model": train_result, "weights": indiv_model.get_feature_importance()}
                    logger.info(f"Individual model trained: {train_result}")
        except Exception as e:
            logger.warning(f"Individual model training failed: {e}")
            indiv_result = {"error": str(e)}

        # --- Post-Ride Feedback Loop ---
        feedback_result = {}
        try:
            # Analyze most recent activity against a hypothetical plan
            if power_metrics_results:
                latest_pm = power_metrics_results[-1]
                planned_tss = 100.0  # Default planned TSS
                actual_tss = latest_pm.get("tss", 0)
                planned_zones = {"Z1": 20.0, "Z2": 50.0, "Z3": 15.0, "Z4": 10.0, "Z5": 5.0}
                actual_zones = latest_pm.get("time_in_zones", {})
                planned_intensity = 0.7
                actual_intensity = latest_pm.get("intensity_factor", 0)

                fb = analyze_post_ride_feedback(
                    planned_tss=planned_tss,
                    actual_tss=actual_tss,
                    planned_zones=planned_zones,
                    actual_zones=actual_zones,
                    planned_intensity=planned_intensity,
                    actual_intensity=actual_intensity,
                    decoupling_drift=dc_result.drift_pct if dc_result else None,
                    w_prime_balance=wp_result.min_balance_pct if wp_result else None,
                )
                feedback_result = feedback_to_dict(fb)
                if fb.plan_mutated:
                    logger.info(f"Feedback: {fb.reason}")
        except Exception as e:
            logger.warning(f"Feedback loop failed: {e}")


    result = {
        "ftp": current_ftp,
        "readiness": readiness_dict,
        "training_load": training_load_result,
        "training_load_history": training_load_history,
        "power_metrics": power_metrics_results,
        "recent_activities": activity_dicts[:5],
        "w_prime": w_prime_results,
        "durability": durability_results,
        "decoupling": decoupling_results,
        "thresholds": thresholds_results,
        "ml_model": ml_result,
        "strain_scores": strain_score_results,
        "pmax_estimates": pmax_results,
        "three_dim_ir": three_dim_model.to_dict(),
        "individual_model": indiv_result,
        "feedback": feedback_result,
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

    # --- ML Model Prediction ---
    ml_prediction = None
    try:
        model_path = VAULT / "data" / "recovery_model.json"
        if model_path.exists():
            from src.analytics.feature_engineering import compute_features

            model = IndividualRecoveryModel()
            if model.load(model_path):
                # Build features from latest wellness data
                readiness = analysis.get("readiness", {})
                training_load = analysis.get("training_load", {})

                wellness_for_ml = [{
                    "date": readiness.get("date", ""),
                    "rmssd": readiness.get("rmssd"),
                    "resting_hr": readiness.get("resting_hr"),
                    "stress": readiness.get("stress"),
                    "sleep_score": readiness.get("sleep_score"),
                    "sleep_hours": readiness.get("sleep_hours"),
                    "body_battery_end": readiness.get("body_battery_end"),
                }]
                activity_for_ml = [{
                    "start_date": readiness.get("date", ""),
                    "tss": training_load.get("atl", 0),
                    "np": 0, "ifr": 0,
                    "w_prime_min_balance": 50,
                    "decoupling_drift": 0,
                }]

                features_df = compute_features(wellness_for_ml, activity_for_ml)
                if not features_df.empty:
                    pred = model.predict(features_df)
                    ml_prediction = {
                        "predicted_prs": pred.predicted_prs,
                        "confidence": pred.confidence,
                        "limiting_factor": pred.limiting_factor,
                        "status": model.metrics.status,
                        "n_samples": model.metrics.n_samples,
                    }
                    logger.info(f"ML prediction: PRS={pred.predicted_prs:.1f} "
                               f"(confidence={pred.confidence:.2f}, "
                               f"limiting={pred.limiting_factor})")
    except Exception as e:
        logger.warning(f"ML prediction failed: {e}")

    # --- Prescription Engine (3-index scoring + guardrails) ---
    prescription_engine_result = None
    try:
        readiness = analysis.get("readiness", {})
        training_load = analysis.get("training_load", {})

        inp = PrescriptionInput(
            rmssd=readiness.get("rmssd"),
            rmssd_baseline=readiness.get("rmssd_mean_30d"),
            rmssd_std=readiness.get("rmssd_std_30d"),
            resting_hr=readiness.get("resting_hr"),
            rhr_baseline=readiness.get("rhr_mean_30d"),
            rhr_std=readiness.get("rhr_std_30d"),
            ctl=training_load.get("ctl"),
            atl=training_load.get("atl"),
            acwr=training_load.get("acwr"),
            planned_tss=80.0,  # default
        )

        output = generate_prescription(inp)
        prescription_engine_result = {
            "readiness_assessment": output.readiness_assessment,
            "daily_plan": output.daily_plan,
            "safety_notes": output.safety_notes,
        }
        logger.info(f"Prescription engine: score={output.readiness_assessment.get('composite_score', 'N/A')}")
    except Exception as e:
        logger.warning(f"Prescription engine failed: {e}")

    # Enrich analysis with ML and engine results
    analysis["ml_prediction"] = ml_prediction
    analysis["prescription_engine"] = prescription_engine_result

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
    parser.add_argument("--visualize", action="store_true", help="Launch the Streamlit dashboard")
    parser.add_argument("--sync-routes", action="store_true", help="Sync route data from FIT files")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    run_all = not any([args.ingest, args.analyze, args.prescribe, args.visualize, args.sync_routes])

    if args.visualize:
        import socket, subprocess
        # Bind to first LAN interface (192.168.x.x or 10.x.x.x), not public
        import ipaddress
        lan_ip = "127.0.0.1"  # fallback
        try:
            import subprocess as sp
            out = sp.check_output(["hostname", "-I"], text=True).strip()
            for ip in out.split():
                try:
                    addr = ipaddress.ip_address(ip)
                    if addr.is_private and not addr.is_loopback:
                        lan_ip = ip
                        break
                except ValueError:
                    pass
        except Exception:
            pass
        subprocess.run([
            sys.executable, "-m", "streamlit", "run",
            str(PROJECT_ROOT / "src" / "visualize.py"),
            "--server.headless", "true",
            "--server.address", lan_ip,
            "--browser.gatherUsageStats", "false",
        ])
        return

    if args.sync_routes:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        db = CyclingDB(DB_PATH)
        raw = config.raw_dir() / "fit"
        counts = sync_routes_from_fit(db, raw)
        db.close()
        print(f"Route sync complete: {counts}")
        return

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