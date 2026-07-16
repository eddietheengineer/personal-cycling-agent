"""
Streamlit visualization dashboard for cycling telemetry data.

Launch with:
    streamlit run src/visualize.py
    or
    python3 -m src.main --visualize

Requires plotly and streamlit in requirements.txt.

NOTE on units from Garmin Connect:
  - duration is stored in milliseconds
  - distance is stored in centimeters
  - speed in activity_streams is m/s
  - elapsed in activity_streams is seconds
"""

import os
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Ensure project root is on sys.path so `src.*` imports work regardless of how
# the script is invoked (streamlit run sets cwd to the script's directory).
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src import config
from src.db.store import CyclingDB
from src.analytics.training_load import compute_training_load_history
from src.ui_helpers import (
    _build_zone_chart,
    _downsample,
    _elapsed_to_minutes,
    _format_duration,
    _distance_km,
    _make_zones,
    _parse_profile_text,
    _stream_id,
    _zone_for_value,
    _ZONE_RANGES,
    _HR_RANGES,
    _LIGHT_COLORS,
    _DARK_COLORS,
)


# Sync progress helpers (background thread → session state bridge)
# ---------------------------------------------------------------------------
# IMPORTANT: Background threads CANNOT write to st.session_state (no ScriptRunContext).
# BackgroundSync/BackgroundTask store progress internally in TaskResult (thread-safe).
# The UI block-waits on the main thread, reading snapshot() and updating st.status().

def _sync_progress_callback(pct: int, msg: str) -> None:
    """Write progress to session state. Called ONLY from main thread."""
    entry = f"[{pct}%] {msg}"
    log = st.session_state.sync_log
    # Skip if the last log entry has the same message text (different pct only)
    if log:
        last_msg = log[-1].split("] ", 1)[-1] if "] " in log[-1] else log[-1]
        if last_msg == msg:
            # Update progress without duplicating the log line
            log[-1] = entry
            st.session_state.sync_log = log
            st.session_state.sync_progress = pct
            return
    log.append(entry)
    # Cap log to avoid websocket frame overflow on long historical syncs
    if len(log) > 500:
        st.session_state.sync_log = log[-500:]
    st.session_state.sync_progress = pct


def _wait_for_task(bg, syncing_key="syncing", rearsing_key=None, sync_mode_key="sync_mode") -> dict | None:
    """Block-wait for a background task to complete, updating progress in real-time.

    Uses st.status() for live progress updates. Blocks the main thread until
    the background task completes or fails.

    Returns the result dict on success, None on failure (error stored in session state).
    """
    import time

    with st.status("Sync in progress...", expanded=True) as status:
        # Create stable placeholders that update in-place each loop iteration
        progress_placeholder = st.empty()

        # Create log expander once, with an empty placeholder inside
        with st.expander("Sync Log", expanded=True):
            log_placeholder = st.empty()

        # Seed initial state
        log_placeholder.text_area("", value="Waiting...", height=200, label_visibility="collapsed", disabled=True)

        while True:
            snapshot = bg.snapshot()
            pct = snapshot["progress"]
            stage = snapshot["stage"]

            # Update session state for log display
            _sync_progress_callback(pct, stage)

            # Update status display in-place
            status.update(label=f"Sync: {stage}", state="running", expanded=True)
            progress_placeholder.progress(pct / 100.0)

            # Update log text in-place — newest first so latest is always visible
            log = st.session_state.get("sync_log", [])
            # Show only last 50 lines to keep websocket frames small
            display_lines = list(reversed(log[-50:])) if log else ["Waiting..."]
            log_text = "\n".join(display_lines)
            log_placeholder.text_area("", value=log_text, height=200, label_visibility="collapsed", disabled=True)

            if snapshot["status"] == "completed":
                result = snapshot.get("result", {})
                status.update(label="Sync complete!", state="complete", expanded=False)
                return result
            elif snapshot["status"] == "failed":
                err = snapshot.get("error", "Unknown error")
                status.update(label=f"Sync failed: {err}", state="complete", expanded=True)
                st.session_state.sync_error = err
                return None

            # Check if user cancelled via session state
            if not st.session_state.get(syncing_key) and (rearsing_key is None or not st.session_state.get(rearsing_key)):
                bg.cancel()
                return None

            time.sleep(0.5)


def _render_sync_progress() -> None:
    """Render the sync progress dialog when a sync is running or just completed.

    Called from _render_garmin_setup(), _render_sync_controls(), and _render_dashboard().
    Block-waits on the background task, showing live progress via st.status().
    """
    syncing = st.session_state.get("syncing")
    rearsing = st.session_state.get("rearsing")
    if not syncing and not rearsing:
        return

    # Ensure session state keys exist
    if "sync_log" not in st.session_state:
        st.session_state.sync_log = []
    if "sync_progress" not in st.session_state:
        st.session_state.sync_progress = 0

    sync_mode = st.session_state.get("sync_mode", "")
    result = None

    # Block-wait for Garmin sync
    if syncing and sync_mode != "prescribe":
        from src.tasks.worker import get_default_sync
        bg = get_default_sync()
        if bg is None:
            # Stale session state (e.g., container restart) — clear and exit
            st.session_state.syncing = False
            st.rerun()
        result = _wait_for_task(bg, syncing_key="syncing")

        if result is not None:
            # Generate readiness explanation for coach page syncs
            if sync_mode == "update" and result.get("analysis"):
                try:
                    analyze_result = result["analysis"]
                    readiness_explanation = _generate_readiness_explanation(analyze_result)
                    _save_readiness_explanation(readiness_explanation, analyze_result)
                    result["analysis"]["readiness_explanation"] = readiness_explanation
                except Exception:
                    pass  # Non-fatal: explanation is cosmetic

            st.session_state.sync_result = result
            st.session_state.syncing = False
            st.rerun()
        else:
            st.session_state.syncing = False
            st.rerun()

    # Block-wait for BackgroundTask (reparse, prescribe)
    if rearsing or (syncing and sync_mode == "prescribe"):
        from src.tasks.worker import get_default_task
        bg = get_default_task()
        result = _wait_for_task(bg, syncing_key="syncing", rearsing_key="rearsing")

        if result is not None:
            st.session_state.sync_result = result
            st.session_state.syncing = False
            st.session_state.rearsing = False
            st.rerun()
        else:
            st.session_state.syncing = False
            st.session_state.rearsing = False
            st.rerun()

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
# Handle Home Assistant Ingress path prefix
# Streamlit checks HASSIO_INGRESS env var for native ingress support
st.set_page_config(page_title="Cycling Agent", layout="wide")


# ---------------------------------------------------------------------------
# DB connection (persisted across reruns via session_state)
# ---------------------------------------------------------------------------
if "db" not in st.session_state:
    try:
        config.setup()
        db_path = config.db_path("cycling_agent.sqlite")
        if not db_path.exists():
            st.error(f"Database not found at `{db_path}`. Run `--ingest` first.")
            st.stop()
        st.session_state.db = CyclingDB(str(db_path))
    except Exception as exc:
        st.error(f"Failed to open database: {exc}")
        st.stop()

db = st.session_state.db


# ---------------------------------------------------------------------------
# Sidebar navigation (compact icon-based)
# ---------------------------------------------------------------------------
st.sidebar.markdown(
    "<style>"
    ".stSidebar .stButton button {width: 100%; text-align: left; padding: 8px 12px;}"
    ".nav-section {font-size: 0.7em; text-transform: uppercase; color: #666; "
    "padding: 12px 0 4px 12px; letter-spacing: 0.05em;}"
    "</style>",
    unsafe_allow_html=True,
)

if "nav_page" not in st.session_state:
    st.session_state.nav_page = "Dashboard"

pages = [
    ("🏠 Dashboard", "Dashboard"),
    ("📋 Activities", "Activity Detail"),
    ("📈 Trends", "Trends"),
    ("🗺 Map", "Map"),
    ("👤 Profile", "Profile"),
    ("⚙️ Settings", "Settings"),
]

for label, page_id in pages:
    active = st.session_state.nav_page == page_id
    if st.sidebar.button(label, use_container_width=True, type="primary" if active else "secondary",
                         key=f"nav_{page_id}"):
        st.session_state.nav_page = page_id
        st.rerun()

nav_page = st.session_state.nav_page

# ---------------------------------------------------------------------------
# Zone helpers (theme-aware; kept here due to Streamlit dependency)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dashboard — primary view
# ---------------------------------------------------------------------------
def _render_dashboard():
    """Single-page dashboard: week strip, readiness, check-in, coach chat."""
    from src.analytics.weekly_planner import generate_weekly_plan, save_weekly_plan, load_weekly_plan
    from src.memory.journal import load_recent, extract_memories, append_entry

    st.title("Cycling Dashboard")

    # ── Sync progress (sidebar sync button) ─────────────────────────────
    _render_sync_progress()

    # ── 7-Day Training Calendar ─────────────────────────────────────────
    _render_week_strip()

    st.divider()

    # ── Readiness Summary ───────────────────────────────────────────────
    _render_readiness_card()

    st.divider()

    # ── Check-in ────────────────────────────────────────────────────────
    _render_dashboard_checkin()

    st.divider()

    # ── Coach Chat ──────────────────────────────────────────────────────
    _render_dashboard_coach()


def _render_week_strip():
    """Compact horizontal 7-day week strip with generate buttons."""
    from src.analytics.weekly_planner import generate_weekly_plan, save_weekly_plan, load_weekly_plan

    plan = load_weekly_plan()

    # Header row with sync + generate buttons
    h_cols = st.columns([4, 1, 1, 1])
    with h_cols[0]:
        st.markdown("**7-Day Plan**", help="Shows today + next 6 days")
        if plan:
            st.caption(f"Plan from {plan.week_start} · {plan.readiness_summary}")
    with h_cols[1]:
        if st.button("🔄 Sync", use_container_width=True, key="sync_dash"):
            if not st.session_state.get("syncing"):
                from src.tasks.worker import background_sync
                background_sync(
                    days=1,
                    unbounded=False,
                    run_analyze_after=True,
                    run_prescribe_after=True,
                )
                st.session_state.syncing = True
                st.session_state.sync_mode = "dashboard"
                st.session_state.sync_days = 1
                st.session_state.sync_result = None
                st.session_state.sync_error = None
                st.session_state.sync_log = []
                st.session_state.sync_progress = 0
                st.rerun()
    with h_cols[2]:
        if st.button("📊 Rules", type="primary", use_container_width=True, key="gen_rules_dash"):
            try:
                p = generate_weekly_plan()
                save_weekly_plan(p)
                st.session_state.week_generated = True
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")
    with h_cols[3]:
        if st.button("🤖 AI", use_container_width=True, key="gen_ai_dash"):
            try:
                from src.analytics.weekly_planner import generate_ai_plan
                p = generate_ai_plan()
                save_weekly_plan(p)
                st.session_state.week_generated = True
                st.rerun()
            except Exception as e:
                st.error(f"AI failed: {e}")

    if st.session_state.get("week_generated"):
        st.success("Plan generated!")
        st.session_state.week_generated = False

    if not plan:
        st.info("Click **Rules** or **AI** above to generate your weekly plan.")
        return

    zone_colors = {
        "rest": "#555", "recovery": "#4caf50", "endurance": "#2196f3",
        "threshold": "#ff9800", "vo2": "#f44336", "anaerobic": "#9c27b0", "mixed": "#00bcd4",
    }
    weather_icons = {"clear": "☀️", "cloudy": "⛅", "rain": "🌧", "snow": "❄️", "storm": "⛈"}
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Fetch fresh weather so every card always has data
    from src.services.weather import get_location, get_weekly_forecast
    forecast_map: dict[str, dict] = {}
    location = get_location()
    if location:
        for f in get_weekly_forecast(location[0], location[1]):
            forecast_map[f.get("date", "")] = f

    cols = st.columns(7)
    for i, day in enumerate(plan.days):
        col = cols[i]
        color = zone_colors.get(day.session_type, "#555")
        is_today = day.date == date.today().isoformat()

        # Weather: fresh forecast first, fall back to plan data
        fc = forecast_map.get(day.date, {})
        if not fc and day.weather_condition:
            fc = {"condition": day.weather_condition, "temp_max": day.weather_temp_max,
                  "temp_min": day.weather_temp_min, "precipitation_prob": day.weather_precip}
        w_icon = weather_icons.get(fc.get("condition", ""), "🌤")
        tmax_f = fc.get("temp_max", 0)
        tmin_f = fc.get("temp_min", 0)
        w_precip = fc.get("precipitation_prob", 0)

        if is_today:
            col.markdown(f"**{day_labels[day.weekday]}**", help=f"{day.date}")
        else:
            col.markdown(day_labels[day.weekday], help=f"{day.date}")

        # Weather row — always at top
        col.markdown(f"{w_icon} {tmax_f:.0f}°F / {tmin_f:.0f}°F  {w_precip}%", help=fc.get("condition", ""))

        if day.rest_day:
            col.markdown("<div style='color:#666; font-size:0.85em;'>Rest</div>", unsafe_allow_html=True)
        else:
            indoor = "🏠" if day.indoor else "🚴"
            col.markdown(f"<div style='color:{color}; font-weight:600;'>{indoor} {day.session_type.title()}</div>", unsafe_allow_html=True)
            col.caption(f"{day.duration_min}min · TSS {day.target_tss:.0f}")
            if day.description:
                col.caption(day.description)

    # Projected fitness/fatigue/load chart
    if plan:
        import plotly.graph_objects as go

        # Use plan series if available, otherwise compute from current analysis
        if plan.ctl_series:
            ctl_s, atl_s, tsb_s = plan.ctl_series, plan.atl_series, plan.tsb_series
        else:
            from src.analytics.weekly_planner import _project_ctl_atl
            from src import config as cfg
            result_path = cfg.vault_path() / "data" / "latest_analysis.json"
            ctl_val, atl_val = 80.0, 60.0
            if result_path.exists():
                import json
                with open(result_path) as f:
                    analysis = json.load(f)
                tl = analysis.get("training_load", {})
                ctl_val = tl.get("ctl", 80.0)
                atl_val = tl.get("atl", 60.0)
            daily_tss = [d.target_tss for d in plan.days]
            ctl_s, atl_s = _project_ctl_atl(ctl_val, atl_val, daily_tss)
            tsb_s = [c - a for c, a in zip(ctl_s, atl_s)]

        labels = [f"{d.date.split('-')[1]}/{d.date.split('-')[2]}" for d in plan.days]

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=labels, y=ctl_s, name="CTL (Fitness)",
                                  line=dict(color="#2196f3", width=2), mode="lines+markers"))
        fig.add_trace(go.Scatter(x=labels, y=atl_s, name="ATL (Fatigue)",
                                  line=dict(color="#f44336", width=2), mode="lines+markers"))
        fig.add_trace(go.Scatter(x=labels, y=tsb_s, name="TSB (Form)",
                                  line=dict(color="#4caf50", width=2), mode="lines+markers"))

        fig.update_layout(
            height=200, margin=dict(l=50, r=20, t=10, b=30),
            xaxis_title="", yaxis_title="",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(size=11),
        )
        fig.update_xaxes(showgrid=False)
        fig.update_yaxes(showgrid=True, gridcolor="#333")
        st.plotly_chart(fig, use_container_width=True)


def _render_readiness_card():
    """Compact readiness status card with metrics."""
    from src import config as cfg

    result_path = cfg.vault_path() / "data" / "latest_analysis.json"
    if not result_path.exists():
        st.info("No analysis data yet. Use **Update Latest Data** in Settings.")
        return

    try:
        import json
        with open(result_path) as f:
            analysis = json.load(f)
    except Exception:
        return

    readiness = analysis.get("readiness", {})
    training_load = analysis.get("training_load", {})
    cp = analysis.get("cp")

    state = readiness.get("state", "unknown").replace("_", " ").title()
    score = readiness.get("composite_score", 0)
    if score >= 70:
        color = "#4caf50"
    elif score >= 50:
        color = "#ff9800"
    else:
        color = "#f44336"

    st.markdown(f"""
    <div style="background:{color}15; border-left: 4px solid {color};
                 padding: 12px 20px; border-radius: 4px;">
        <div style="font-size: 1.1em; font-weight: 600; color: {color};">
            {state} — Readiness {score:.0f}/100
        </div>
    </div>
    """, unsafe_allow_html=True)

    m_cols = st.columns(6)
    m_cols[0].metric("CP", f"{cp:.0f}W" if cp else "—")
    m_cols[1].metric("CTL", f"{training_load.get('ctl', 0):.0f}")
    m_cols[2].metric("ATL", f"{training_load.get('atl', 0):.0f}")
    m_cols[3].metric("HRV", f"{readiness.get('rmssd', '—')}")
    m_cols[4].metric("RHR", f"{readiness.get('resting_hr', '—')}")

    rec = readiness.get("recommendation", "")
    if rec:
        st.caption(rec)

        # Fitness/Fatigue/Form indicators
        ctl = training_load.get("ctl", 0)
        atl = training_load.get("atl", 0)
        tsb = training_load.get("tsb", 0)

        c_cols = st.columns(3)
        # CTL thresholds per Coggan (TrainingPeaks Performance Manager):
        # <100 undertraining, 100-150 optimal, >150 unsustainable
        if ctl > 150:
            ctl_status = "🟠 Very high (unsustainable long-term)"
        elif ctl > 100:
            ctl_status = "🟢 Optimal fitness base"
        elif ctl > 50:
            ctl_status = "🟡 Building (room to add volume)"
        else:
            ctl_status = "🔴 Low (undertraining)"
        c_cols[0].markdown(f"**CTL {ctl:.0f}** {ctl_status}")

        # ATL is recent load — interpret relative to CTL via TSB
        if tsb < -20:
            atl_status = "🔴 Very high (heavy fatigue)"
        elif tsb < 0:
            atl_status = "🟠 Elevated (fatigued)"
        elif tsb < 10:
            atl_status = "🟡 Moderate (normal training)"
        else:
            atl_status = "🟢 Low (recovered)"
        c_cols[1].markdown(f"**ATL {atl:.0f}** {atl_status}")

        # TSB thresholds per Coggan: <−10 not fresh, −10 to +10 neutral, >+10 fresh
        if tsb > 10:
            tsb_status = "🟢 Fresh"
        elif tsb > -10:
            tsb_status = "🟡 Neutral"
        else:
            tsb_status = "🔴 Fatigued"
        c_cols[2].markdown(f"**TSB {tsb:.0f}** {tsb_status}")


def _render_dashboard_checkin():
    """Compact check-in form, expanded if no check-in today."""
    from src.db.store import CyclingDB

    today_str = date.today().isoformat()
    existing = db.get_morning_checkin(today_str)
    expanded = not bool(existing)

    label = "✅ Checked in" if existing else "📝 Morning Check-in"
    with st.expander(label, expanded=expanded):
        if existing:
            st.caption(f"Soreness {existing.get('soreness')} · Stress {existing.get('stress')} · Sleep {existing.get('sleep_quality')} · Mood {existing.get('mood')} · Energy {existing.get('energy')}")
            notes = existing.get("notes")
            if notes:
                st.caption(f"📝 {notes}")
            return

        if "checkin_date" not in st.session_state:
            st.session_state.checkin_date = today_str

        with st.form("dash_checkin_form", clear_on_submit=False):
            c1, c2 = st.columns(2)
            with c1:
                soreness = st.select_slider("Soreness", options=[1,2,3,4,5], value=3,
                    format_func=lambda v: {1:"None",2:"Mild",3:"Moderate",4:"High",5:"Severe"}[v])
                stress = st.select_slider("Life Stress", options=[1,2,3,4,5], value=3,
                    format_func=lambda v: {1:"None",2:"Low",3:"Moderate",4:"High",5:"Overwhelming"}[v])
                sleep_quality = st.select_slider("Sleep Quality", options=[1,2,3,4,5], value=3,
                    format_func=lambda v: {1:"Terrible",2:"Poor",3:"Okay",4:"Good",5:"Great"}[v])
            with c2:
                mood = st.select_slider("Mood", options=[1,2,3,4,5], value=3,
                    format_func=lambda v: {1:"Terrible",2:"Low",3:"Okay",4:"Good",5:"Great"}[v])
                energy = st.select_slider("Energy", options=[1,2,3,4,5], value=3,
                    format_func=lambda v: {1:"None",2:"Low",3:"Moderate",4:"High",5:"Peak"}[v])
                motivation = st.select_slider("Motivation", options=[1,2,3,4,5], value=3,
                    format_func=lambda v: {1:"None",2:"Low",3:"Moderate",4:"High",5:"Peak"}[v])

            cb1, cb2, cb3 = st.columns(3)
            caffeine = cb1.checkbox("☕ Caffeine")
            alcohol = cb2.checkbox("🍺 Alcohol")
            late_meals = cb3.checkbox("🌙 Late Meals")

            notes = st.text_area("Notes", placeholder="Travel day, feeling off...", key="dash_checkin_notes")

            if st.form_submit_button("Save Check-in", type="primary"):
                db.store_morning_checkin({
                    "date": today_str, "soreness": soreness, "stress": stress,
                    "sleep_quality": sleep_quality, "mood": mood, "energy": energy,
                    "motivation": motivation, "caffeine": caffeine, "alcohol": alcohol,
                    "late_meals": late_meals, "notes": notes,
                })
                st.rerun()


def _render_dashboard_coach():
    """Compact coach chat section."""
    if "coach_messages" not in st.session_state:
        st.session_state.coach_messages = []

    st.markdown("**💬 Coach**")

    # Show last few messages
    chat_area = st.container()
    with chat_area:
        for msg in st.session_state.coach_messages[-6:]:
            if msg["role"] == "user":
                st.markdown(f"**You:** {msg['content']}")
            else:
                st.markdown(f"*Coach:* {msg['content']}")

    # Input row
    ic1, ic2, ic3 = st.columns([4, 1, 1])
    with ic1:
        user_input = st.text_input("Ask your coach...", key="dash_coach_input",
            placeholder="Should I train today? How's my recovery?")
    with ic2:
        send_clicked = st.button("Send", type="primary", use_container_width=True, key="dash_coach_send")
    with ic3:
        clear_clicked = st.button("Clear", use_container_width=True, key="dash_coach_clear")

    if clear_clicked:
        st.session_state.coach_messages = []
        st.rerun()

    if send_clicked and user_input.strip():
        st.session_state.coach_messages.append({"role": "user", "content": user_input.strip()})

        from src.agent import prompt_builder, llm_client
        from src import config as cfg

        analysis = None
        result_path = cfg.vault_path() / "data" / "latest_analysis.json"
        if result_path.exists():
            try:
                import json
                with open(result_path) as f:
                    analysis = json.load(f)
            except Exception:
                pass

        system_prompt = prompt_builder.build_system_prompt(
            readiness=analysis.get("readiness") if analysis else None,
            recent_activities=analysis.get("recent_activities") if analysis else None,
            thresholds=analysis.get("thresholds") if analysis else None,
            w_prime=analysis.get("w_prime") if analysis else None,
            durability=analysis.get("durability") if analysis else None,
            decoupling=analysis.get("decoupling") if analysis else None,
        )

        journal_context = load_recent(30)
        if journal_context:
            system_prompt = f"## Memory Journal\n{journal_context}\n\n{system_prompt}"

        conv_text = "\n".join(
            f"{m['role'].upper()}: {m['content']}"
            for m in st.session_state.coach_messages
        )
        full_prompt = f"{system_prompt}\n\nConversation:\n{conv_text}\n\nASSISTANT:"

        try:
            with st.spinner("Coach is thinking..."):
                response = llm_client.generate(full_prompt, stream=False)
            st.session_state.coach_messages.append({"role": "assistant", "content": response})

            def _extract_and_save():
                try:
                    bullets = extract_memories(user_input.strip(), response)
                    for bullet in bullets:
                        append_entry(bullet)
                except Exception:
                    pass
            import threading
            threading.Thread(target=_extract_and_save, daemon=True).start()
        except Exception as e:
            st.error(f"Coach error: {e}")

        st.rerun()
def _zone_colors():
    """Return zone color list matching current Streamlit theme."""
    theme = st.get_option("theme.base")
    if theme == "dark":
        return _DARK_COLORS
    return _LIGHT_COLORS




# ---------------------------------------------------------------------------
# Morning Check-in
# ---------------------------------------------------------------------------
def _render_checkin():
    st.header("Morning Check-in")
    st.caption("Quick daily check-in to improve training recommendations")

    # Default to today
    if "checkin_date" not in st.session_state:
        st.session_state.checkin_date = date.today().isoformat()

    selected_date = st.date_input(
        "Date", value=date.fromisoformat(st.session_state.checkin_date),
        key="checkin_date_input"
    )
    checkin_date = selected_date.isoformat()

    # Load existing check-in if any
    existing = db.get_morning_checkin(checkin_date)

    # Map actual DB columns to form fields
    if existing:
        defaults = {
            "soreness": existing.get("soreness") or 3,
            "stress": existing.get("stress") or 3,
            "sleep_quality": existing.get("sleep_quality") or 3,
            "mood": existing.get("mood") or 3,
            "energy": existing.get("energy") or 3,
            "motivation": existing.get("motivation") or 3,
            "caffeine": bool(existing.get("caffeine")),
            "alcohol": bool(existing.get("alcohol")),
            "late_meals": bool(existing.get("late_meals")),
            "notes": existing.get("notes") or "",
        }
    else:
        defaults = {
            "mood": 3, "energy": 3, "motivation": 3,
            "caffeine": False, "alcohol": False, "late_meals": False,
            "notes": "",
        }

    with st.form("checkin_form", clear_on_submit=False):
        st.subheader("How do you feel? (1-5)")
        c1, c2 = st.columns(2)
        with c1:
            soreness = st.select_slider("Soreness", options=[1,2,3,4,5], value=defaults["soreness"],
                                         format_func=lambda v: {1:"None",2:"Mild",3:"Moderate",4:"High",5:"Severe"}[v])
            stress = st.select_slider("Life Stress", options=[1,2,3,4,5], value=defaults["stress"],
                                       format_func=lambda v: {1:"None",2:"Low",3:"Moderate",4:"High",5:"Overwhelming"}[v])
            sleep_quality = st.select_slider("Sleep Quality", options=[1,2,3,4,5], value=defaults["sleep_quality"],
                                              format_func=lambda v: {1:"Terrible",2:"Poor",3:"Okay",4:"Good",5:"Great"}[v])
        with c2:
            mood = st.select_slider("Mood", options=[1,2,3,4,5], value=defaults["mood"],
                                     format_func=lambda v: {1:"Terrible",2:"Low",3:"Okay",4:"Good",5:"Great"}[v])
            energy = st.select_slider("Energy", options=[1,2,3,4,5], value=defaults["energy"],
                                       format_func=lambda v: {1:"None",2:"Low",3:"Moderate",4:"High",5:"Peak"}[v])
            motivation = st.select_slider("Motivation", options=[1,2,3,4,5], value=defaults["motivation"],
                                          format_func=lambda v: {1:"None",2:"Low",3:"Moderate",4:"High",5:"Peak"}[v])

        st.subheader("Lifestyle (yes/no)")
        c1, c2, c3 = st.columns(3)
        with c1:
            caffeine = st.checkbox("Morning Caffeine", value=defaults["caffeine"])
        with c2:
            alcohol = st.checkbox("Alcohol Yesterday", value=defaults["alcohol"])
        with c3:
            late_meals = st.checkbox("Late Meals", value=defaults["late_meals"])

        notes = st.text_area(
            "Notes (optional)", value=defaults["notes"],
            placeholder="Travel day, feeling under the weather, big meeting tomorrow...",
            key="checkin_notes",
        )

        submitted = st.form_submit_button("Save Check-in")
        if submitted:
            db.store_morning_checkin({
                "date": checkin_date,
                "soreness": soreness,
                "stress": stress,
                "sleep_quality": sleep_quality,
                "mood": mood,
                "energy": energy,
                "motivation": motivation,
                "caffeine": caffeine,
                "alcohol": alcohol,
                "late_meals": late_meals,
                "notes": notes,
            })
            st.success(f"Check-in saved for {checkin_date}!")

    # Show history
    st.subheader("Recent Check-ins")
    history = db.get_morning_checkins(limit=7)
    if history:
        rows = []
        for h in sorted(history, key=lambda r: r["date"], reverse=True):
            stress_val = h.get("stress")
            rows.append({
                "Date": h["date"],
                "Soreness": h.get("soreness"),
                "Stress": stress_val,
                "Sleep": h.get("sleep_quality"),
                "Mood": h.get("mood"),
                "Energy": h.get("energy"),
                "Motivation": h.get("motivation"),
                "Caffeine": "☕" if h.get("caffeine") else "—",
                "Alcohol": "🍺" if h.get("alcohol") else "—",
                "Late Meals": "🌙" if h.get("late_meals") else "—",
                "Notes": h.get("notes") or "—",
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info("No check-ins yet. Fill out the form above to get started!")

# ---------------------------------------------------------------------------
# Activity Detail tab
# ---------------------------------------------------------------------------
def _render_activity_detail():
    activities = db.get_activities()

    if not activities:
        st.info("No activities found. Run `--ingest` first.")
        return

    # Build display labels sorted by date descending (most recent first)
    options = []
    id_map = {}
    for a in activities:
        label = f"{a['start_date'][:10]} — {a['activity_type']}"
        options.append(label)
        id_map[label] = a["id"]

    selected_label = st.selectbox("Select activity", options)
    selected_id = id_map[selected_label]

    # Fetch combined activity + computed metrics
    combined = db.get_activity_with_metrics(selected_id)
    if combined is None:
        st.warning("Activity not found in database.")
        return

    # -- Metadata cards --
    st.subheader(f"Activity: {combined['activity_type']}")
    st.caption(f"ID: {combined['id']}")

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Date", combined["start_date"][:10])
    col2.metric("Duration", _format_duration(combined.get("duration")))
    col3.metric("Distance", _distance_km(combined.get("distance")))
    col4.metric("Calories", f"{combined.get('calories', 0):.0f}" if combined.get("calories") else "—")

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Avg Power", f"{combined.get('average_power', 0):.0f} W" if combined.get("average_power") else "—")
    col6.metric("Max Power", f"{combined.get('max_power', 0):.0f} W" if combined.get("max_power") else "—")
    col7.metric("Avg HR", f"{combined.get('average_hr', 0):.0f} bpm" if combined.get("average_hr") else "—")
    col8.metric("Max HR", f"{combined.get('max_hr', 0):.0f} bpm" if combined.get("max_hr") else "—")

    # Computed metrics (if available)
    computed_fields = {
        "CP Used": ("cp_used", "W"),
        "Normalized Power": ("normalized_power", "W"),
        "Intensity Factor": ("intensity_factor", ""),
        "TSS": ("tss", ""),
        "Variability Index": ("variability_index", ""),
        "W' Capacity": ("w_prime_capacity", "kJ"),
        "Decoupling Drift": ("decoupling_drift", "%"),
    }

    computed_cols = st.columns(len(computed_fields))
    for idx, (label, (key, unit)) in enumerate(computed_fields.items()):
        val = combined.get(key)
        if val is not None:
            if key == "intensity_factor":
                computed_cols[idx].metric(label, f"{val:.2f}")
            elif key == "decoupling_drift":
                computed_cols[idx].metric(label, f"{val:.1f}%")
            elif unit == "kJ":
                computed_cols[idx].metric(label, f"{val:.1f} kJ")
            else:
                computed_cols[idx].metric(label, f"{val:.1f} {unit}")
        else:
            computed_cols[idx].metric(label, "—")

    # -- Stream charts --
    stream_metrics = ["power", "heart_rate", "speed", "cadence", "altitude"]
    metric_labels = {
        "power": "Power (W)",
        "heart_rate": "Heart Rate (bpm)",
        "speed": "Speed (km/h)",
        "cadence": "Cadence (rpm)",
        "altitude": "Altitude (m)",
    }

    sid = _stream_id(selected_id)

    # Determine zone thresholds from activity data
    ftp = combined.get("cp_used") or combined.get("average_power") or 0.0
    max_hr = combined.get("max_hr") or 0.0

    has_any_stream = False
    for metric in stream_metrics:
        rows = db.get_activity_streams(sid, metric)
        if not rows:
            continue

        has_any_stream = True
        elapsed = [r["elapsed"] for r in rows]
        values = [r["value"] for r in rows]
        elapsed, values = _downsample(elapsed, values)

        # Garmin speed is in m/s; convert to km/h for display
        if metric == "speed":
            values = [v * 3.6 for v in values]

        y_label = metric_labels.get(metric, metric)
        title = y_label

        if metric == "power" and ftp > 0:
            colors = _zone_colors()
            zones = _make_zones(_ZONE_RANGES, colors)
            fig = _build_zone_chart(elapsed, values, ftp, zones, y_label, title, st)
            st.plotly_chart(fig, width="stretch")
        elif metric == "heart_rate" and max_hr > 0:
            colors = _zone_colors()
            zones = _make_zones(_HR_RANGES, colors)
            fig = _build_zone_chart(elapsed, values, max_hr, zones, y_label, title, st)
            st.plotly_chart(fig, width="stretch")
        else:
            fig = px.line(
                x=[_elapsed_to_minutes(e) for e in elapsed],
                y=values,
                labels={"x": "Elapsed (min)", "y": y_label},
                title=title,
                template="plotly_white",
            )
            fig.update_traces(line=dict(width=1.5))
            fig.update_layout(height=320, margin=dict(l=50, r=20, t=40, b=40))
            st.plotly_chart(fig, width="stretch")

    if not has_any_stream:
        st.warning("No stream data for this activity.")


# ---------------------------------------------------------------------------
# Trends tab
# ---------------------------------------------------------------------------
def _render_trends():
    # Date range presets
    wellness_rows = db.get_trend_data("wellness", ["date"])
    if not wellness_rows:
        st.info("No wellness data found. Run sync first.")
        return

    all_dates = [r["date"] for r in wellness_rows]
    min_date = min(all_dates)
    max_date = max(max(all_dates), date.today().isoformat())

    today = date.today()
    this_year_start = date(today.year, 1, 1).isoformat()
    this_year_end = today.isoformat()

    preset_map = {
        "This Year": (date(today.year, 1, 1), today),
        "Last 90 Days": (today - timedelta(days=90), today),
        "Last 30 Days": (today - timedelta(days=30), today),
        "All Time": (date.fromisoformat(min_date), today),
    }

    selected_preset = st.selectbox("Time range", list(preset_map.keys()), index=0)
    preset_start, preset_end = preset_map[selected_preset]

    oldest = preset_start.isoformat()
    newest = preset_end.isoformat()

    # ---- Gather activity data ----
    # CP chart: only show data within selected range
    metrics_rows = db.get_activity_metrics_by_date(oldest, newest)
    cp_chart_data = []

    for row in metrics_rows:
        sd = row.get("start_date")
        if not sd:
            continue
        if row.get("cp_used") is not None:
            cp_chart_data.append({"date": sd, "cp_used": row["cp_used"]})

    # CTL/ATL/TSB: always compute from full history, then filter to range.
    # This ensures CTL/ATL reflect the true accumulated training load
    # even when viewing a zoomed-in window (e.g. last 90 days).
    all_metrics = db.get_activity_metrics_by_date()
    tss_by_date: dict[str, float] = {}
    for row in all_metrics:
        sd = row.get("start_date")
        if not sd:
            continue
        if row.get("tss") is not None:
            d = sd[:10]
            tss_by_date[d] = tss_by_date.get(d, 0.0) + row["tss"]

    # ---- Combined CTL / ATL / TSB plot ----
    if tss_by_date:
        sorted_dates = sorted(tss_by_date.keys())
        tss_records = [{"date": d, "tss": tss_by_date[d]} for d in sorted_dates]
        history = compute_training_load_history(tss_records)

        # Filter to selected date range for display
        history = [h for h in history if oldest <= h["date"] <= newest]

        if history:
            df_load = pd.DataFrame(history)

            # Plot historical data
            fig = px.line(
                df_load, x="date", y=["ctl", "atl", "tsb"],
                labels={"value": "", "variable": ""},
                title="CTL · ATL · TSB",
                template="plotly_white",
                color_discrete_map={
                    "ctl": "#1f77b4",
                    "atl": "#ff7f0e",
                    "tsb": "#2ca02c",
                },
            )
            fig.update_traces(line=dict(width=2))
            fig.update_layout(height=350, legend=dict(title=""))

            # Overlay projected plan data as dashed lines
            from src.analytics.weekly_planner import load_weekly_plan
            plan = load_weekly_plan()
            if plan and plan.ctl_series:
                plan_dates = [d.date for d in plan.days]
                colors = {"ctl": "#1f77b4", "atl": "#ff7f0e", "tsb": "#2ca02c"}
                series_map = {"ctl": plan.ctl_series, "atl": plan.atl_series, "tsb": plan.tsb_series}
                for metric in ["ctl", "atl", "tsb"]:
                    import plotly.graph_objects as go
                    fig.add_trace(go.Scatter(
                        x=plan_dates, y=series_map[metric],
                        mode="lines", name=f"{metric} (projected)",
                        line=dict(color=colors[metric], width=2, dash="dash"),
                        opacity=0.7,
                    ))

            # Add zone bands for TSB
            fig.add_hline(y=10, line_dash="dot", line_color="#2ca02c", opacity=0.4, annotation_text="Fresh")
            fig.add_hline(y=-10, line_dash="dot", line_color="#dc3545", opacity=0.4, annotation_text="Tired")
            fig.add_hrect(y0=-10, y1=10, fillcolor="grey", opacity=0.06, layer="below")

            st.plotly_chart(fig, width="stretch")

    # ---- Critical Power ----
    if cp_chart_data:
        df_cp = pd.DataFrame(cp_chart_data)
        fig = px.line(
            df_cp, x="date", y="cp_used",
            labels={"cp_used": "CP (W)"},
            title="Critical Power",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=2, color="#9467bd"))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")

    # ---- Wellness trends ----
    wellness_data = db.get_trend_data(
        "wellness",
        ["date", "rmssd", "resting_hr", "weight", "stress", "sleep_hours", "sleep_score"],
        oldest,
        newest,
    )

    if not wellness_data:
        return

    df_wellness = pd.DataFrame(wellness_data)

    # HRV (RMSSD)
    rmssd_rows = df_wellness[df_wellness["rmssd"].notna()]
    if not rmssd_rows.empty:
        fig = px.line(
            rmssd_rows, x="date", y="rmssd",
            labels={"rmssd": "RMSSD (ms)"},
            title="HRV (RMSSD)",
            template="plotly_white",
        )
        mean_rmssd = rmssd_rows["rmssd"].mean()
        fig.add_hline(y=mean_rmssd, line_dash="dash", annotation_text=f"Mean: {mean_rmssd:.0f}")
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")

    # Resting HR
    rhr_rows = df_wellness[df_wellness["resting_hr"].notna()]
    if not rhr_rows.empty:
        fig = px.line(
            rhr_rows, x="date", y="resting_hr",
            labels={"resting_hr": "Resting HR (bpm)"},
            title="Resting Heart Rate",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")

    # Weight
    weight_rows = df_wellness[df_wellness["weight"].notna()]
    if not weight_rows.empty:
        fig = px.line(
            weight_rows, x="date", y="weight",
            labels={"weight": "Weight (kg)"},
            title="Weight",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")

    # Sleep
    sleep_rows = df_wellness[
        df_wellness["sleep_hours"].notna() | df_wellness["sleep_score"].notna()
    ]
    if not sleep_rows.empty:
        fig = go.Figure()
        if sleep_rows["sleep_hours"].notna().any():
            fig.add_trace(go.Scatter(
                x=sleep_rows["date"], y=sleep_rows["sleep_hours"],
                name="Hours", mode="lines",
                line=dict(width=2, color="#9467bd"),
            ))
        if sleep_rows["sleep_score"].notna().any():
            fig.add_trace(go.Scatter(
                x=sleep_rows["date"], y=sleep_rows["sleep_score"],
                name="Score", mode="lines",
                line=dict(width=2, color="#1f77b4"),
            ))
        fig.update_layout(
            title="Sleep",
            height=280,
            template="plotly_white",
            yaxis=dict(title="Hours / Score"),
            legend=dict(title=""),
        )
        st.plotly_chart(fig, width="stretch")

    # Stress
    stress_rows = df_wellness[df_wellness["stress"].notna()]
    if not stress_rows.empty:
        fig = px.line(
            stress_rows, x="date", y="stress",
            labels={"stress": "Stress"},
            title="Daily Stress",
            template="plotly_white",
        )
        fig.update_traces(line=dict(width=1.5))
        fig.update_layout(height=280)
        st.plotly_chart(fig, width="stretch")



# ---------------------------------------------------------------------------
# Map tab
# ---------------------------------------------------------------------------
def _geocode_city(city: str) -> tuple[float, float] | None:
    """Geocode a city name to (lat, lon) using Nominatim. Cached in session_state."""
    cache = st.session_state.setdefault("_geocode_cache", {})
    if city not in cache:
        from geopy.geocoders import Nominatim
        from geopy.exc import GeopyError
        try:
            geolocator = Nominatim(user_agent="personal-cycling-agent")
            location = geolocator.geocode(city)
            if location is not None:
                cache[city] = (location.latitude, location.longitude)
            else:
                cache[city] = None
        except GeopyError:
            cache[city] = None
    return cache.get(city)


def _render_map():
    """Render a map tab with heatmap of route activity."""
    from geopy.distance import geodesic

    st.subheader("Route Map")

    # Sidebar inputs for geographic filter
    city = st.text_input("City", value="Louisville, Kentucky")
    radius_miles = st.slider("Radius (miles)", min_value=10, max_value=500, value=100, step=10)

    # Geocode the city
    center = _geocode_city(city)
    if center is None:
        st.error(f"Could not geocode \"{city}\". Try a more specific address.")
        return

    center_lat, center_lon = center
    st.info(f"Center: {city} ({center_lat:.4f}, {center_lon:.4f}) — Radius: {radius_miles} mi")

    # Get distinct activity IDs with route data
    route_ids = db.conn.execute(
        "SELECT DISTINCT activity_id FROM activity_routes"
    ).fetchall()

    if not route_ids:
        st.info("No route data found. Run --sync-routes first.")
        return

    # For each activity, compute centroid and check if within radius
    matching_ids: list[str] = []
    all_points: list[dict] = []

    for (activity_id,) in route_ids:
        # Get centroid of this activity's route
        centroid = db.conn.execute(
            "SELECT AVG(latitude), AVG(longitude) FROM activity_routes WHERE activity_id = ?",
            (activity_id,),
        ).fetchone()

        if centroid[0] is None or centroid[1] is None:
            continue

        dist = geodesic((center_lat, center_lon), (centroid[0], centroid[1])).miles

        if dist <= radius_miles:
            matching_ids.append(activity_id)
            # Fetch all points for this activity
            points = db.conn.execute(
                "SELECT latitude, longitude FROM activity_routes WHERE activity_id = ? ORDER BY sequence",
                (activity_id,),
            ).fetchall()
            for (lat, lon) in points:
                all_points.append({"lat": lat, "lon": lon, "activity_id": activity_id})

    if not all_points:
        st.info(f"No routes found within {radius_miles} mi of {city}.")
        return

    df = pd.DataFrame(all_points)

    # Stats
    col1, col2, col3 = st.columns(3)
    col1.metric("Activities", len(matching_ids))
    col2.metric("Total Points", len(df))
    col3.metric("Coverage", f"{radius_miles} mi radius")

    # Render map — use scatter_mapbox if token available, fallback to scatter_geo
    mapbox_token = os.environ.get("MAPBOX_TOKEN", "")

    if mapbox_token:
        fig = px.scatter_mapbox(
            df,
            lat="lat",
            lon="lon",
            color="lat",
            color_continuous_scale="Hot",
            title=f"Route Heatmap — {city} ({len(matching_ids)} activities)",
            hover_data={"activity_id": True},
            opacity=0.6,
        )
        fig.update_layout(
            mapbox={"accesstoken": mapbox_token, "style": "open-street-map", "center": {"lat": center_lat, "lon": center_lon}, "zoom": 8},
            margin={"r": 0, "t": 40, "l": 0, "b": 0},
            height=600,
        )
    else:
        fig = px.scatter_geo(
            df,
            lat="lat",
            lon="lon",
            color="lat",
            color_continuous_scale="Hot",
            title=f"Route Heatmap — {city} ({len(matching_ids)} activities) [no Mapbox token]",
            hover_data={"activity_id": True},
            opacity=0.6,
            scope="usa",
        )
        fig.update_layout(
            margin={"r": 0, "t": 40, "l": 0, "b": 0},
            height=600,
        )

    st.plotly_chart(fig, width="stretch")


def _render_profile():
    """Render the athlete profile editing tab."""
    profile_path = config.user_profile_path()

    # Read existing profile into a dict
    profile = {
        "name": os.getenv("ATHLETE_NAME", ""),
        "weight_kg": int(os.getenv("WEIGHT_KG", "0")),
        "height_cm": int(os.getenv("HEIGHT_CM", "0")),
        "discipline": os.getenv("DISCIPLINE", "road"),
        "ftp_watts": int(os.getenv("FTP_WATTS", "0")),
        "max_hr": int(os.getenv("MAX_HR", "0")),
        "resting_hr": int(os.getenv("RESTING_HR", "0")),
        "lt1_power": int(os.getenv("LT1_POWER", "0")),
        "lt2_power": int(os.getenv("LT2_POWER", "0")),
        "primary_goal": os.getenv("PRIMARY_GOAL", ""),
        "secondary_goal": os.getenv("SECONDARY_GOAL", ""),
        "training_days": os.getenv("TRAINING_DAYS", ""),
        "max_session_duration": os.getenv("MAX_SESSION_DURATION", ""),
        "terrain": os.getenv("TERRAIN", ""),
        "bikes": os.getenv("BIKES", ""),
        "power_meter": os.getenv("POWER_METER", ""),
        "hr_monitor": os.getenv("HR_MONITOR", ""),
        "gender": os.getenv("GENDER", "male"),
        "tsb_floor": -10,
    }

    if profile_path.exists():
        _parse_profile_text(profile_path.read_text(), profile)

    st.subheader("Athlete Profile")
    st.caption("Edit your profile below. Changes are saved to your vault directory and used by the analytics engine.")

    with st.form("profile_form", clear_on_submit=False):
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Identity")
            name = st.text_input("Name", value=profile["name"], key="prof_name")
            weight = st.number_input("Weight (kg)", min_value=0, value=profile["weight_kg"], key="prof_weight")
            height = st.number_input("Height (cm)", min_value=0, value=profile["height_cm"], key="prof_height")

            st.subheader("Training")
            disciplines = ["road", "gravel", "MTB", "TT"]
            discipline = st.selectbox("Primary Discipline", disciplines,
                                      index=disciplines.index(profile["discipline"]) if profile["discipline"] in disciplines else 0,
                                      key="prof_discipline")

        with col2:
            st.subheader("Physiological Baselines")
            ftp = st.number_input("FTP (watts)", min_value=0, value=profile["ftp_watts"], key="prof_ftp")
            max_hr = st.number_input("Max HR", min_value=0, value=profile["max_hr"], key="prof_max_hr")
            resting_hr = st.number_input("Resting HR (avg)", min_value=0, value=profile["resting_hr"], key="prof_resting_hr")
            gender = st.selectbox(
                "Gender", ["male", "female"],
                index=["male", "female"].index(profile["gender"]) if profile["gender"] in ["male", "female"] else 0,
                key="prof_gender",
            )
            lt1 = st.number_input("LT1 Power (watts)", min_value=0, value=profile["lt1_power"], key="prof_lt1")
            lt2 = st.number_input("LT2 Power (watts)", min_value=0, value=profile["lt2_power"], key="prof_lt2")

        st.subheader("Goals & Constraints")
        goal_col1, goal_col2 = st.columns(2)
        with goal_col1:
            primary_goal = st.text_input("Primary Goal", value=profile["primary_goal"], key="prof_primary_goal")
            training_days = st.text_input("Available Training Days", value=profile["training_days"],
                                          placeholder="e.g., Mon,Wed,Fri", key="prof_training_days")
        with goal_col2:
            secondary_goal = st.text_input("Secondary Goal", value=profile["secondary_goal"], key="prof_secondary_goal")
            max_session = st.text_input("Max Session Duration", value=profile["max_session_duration"],
                                        placeholder="e.g., 2h", key="prof_max_session")

        terrain = st.text_area("Terrain Notes", value=profile["terrain"],
                                placeholder="e.g., flat, hilly, mountainous", key="prof_terrain")

        st.subheader("Equipment")
        equip_col1, equip_col2 = st.columns(2)
        with equip_col1:
            bikes = st.text_input("Bike(s)", value=profile["bikes"], key="prof_bikes")
        with equip_col2:
            power_meter = st.text_input("Power Meter", value=profile["power_meter"], key="prof_power_meter")
            hr_monitor = st.text_input("HR Monitor", value=profile["hr_monitor"], key="prof_hr_monitor")

        st.subheader("Training Safety")
        st.caption("TSB floor: minimum TSB the planner will allow. Going below this risks injury.")

        tsb_floor = st.slider(
            "TSB Floor", min_value=-30, max_value=10, value=profile["tsb_floor"],
            step=1, key="prof_tsb_floor",
        )
        profile["tsb_floor"] = tsb_floor

        # Color-coded reference bar matching slider range (-30 to +10)
        # -10 is at 50% of the range
        if tsb_floor < -10:
            zone_color, zone = "#dc3545", "Red (Tired)"
        elif tsb_floor <= 10:
            zone_color, zone = "#868e96", "Grey (Zero Form)"
        else:
            zone_color, zone = "#28a745", "Green (Fresh)"

        st.markdown(f"""
        <div style="display:flex;align-items:center;gap:6px;margin-top:4px">
          <span style="font-size:11px;color:#dc3545">Red</span>
          <div style="flex:1;height:6px;border-radius:3px;
            background:linear-gradient(to right, #dc3545 0%, #dc3545 50%, #868e96 50%, #868e96 100%)">
          </div>
          <span style="font-size:11px;color:#868e96">Grey</span>
        </div>
        <div style="text-align:center;font-size:12px;color:{zone_color};font-weight:bold;margin-top:2px">
          {tsb_floor:+d} → {zone}
        </div>
        """, unsafe_allow_html=True)

        st.subheader("Location & Schedule")
        loc_col1, loc_col2 = st.columns(2)
        from src.config.schedule import load_weather_location, save_weather_location
        wl = load_weather_location()
        wl_lat, wl_lon = wl if wl else (0.0, 0.0)
        with loc_col1:
            weather_lat = st.number_input("Latitude", value=wl_lat,
                                          format="%.6f", key="prof_lat")
        with loc_col2:
            weather_lon = st.number_input("Longitude", value=wl_lon,
                                          format="%.6f", key="prof_lon")
        st.caption("Used for weather forecasts. Leave at 0 to auto-detect from Garmin GPS.")

        submitted = st.form_submit_button("Save Profile")

        if submitted:
            content = f"""# Athlete Profile

## Identity
- Name: {name}
- Weight (kg): {int(weight)}
- Height (cm): {int(height)}

## Training History
- Primary discipline: {discipline}

## Physiological Baselines
- FTP (watts): {int(ftp)}
- Max HR: {int(max_hr)}
- Resting HR (avg): {int(resting_hr)}
- Gender: {gender}
- LT1 power (if known): {int(lt1)}
- LT2 power (if known): {int(lt2)}

## Goals & Constraints
- Primary goal: {primary_goal}
- Secondary goal: {secondary_goal}
- Available training days: {training_days}
- Max session duration: {max_session}
- Terrain notes: {terrain}

## Equipment
- Bike(s): {bikes}
- Power meter: {power_meter}
- HR monitor: {hr_monitor}
"""
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(content)
            if weather_lat != 0.0 or weather_lon != 0.0:
                save_weather_location(weather_lat, weather_lon)
            st.success("Profile saved!")

    # Hour availability grid (outside form to avoid re-render issues)
    st.divider()
    _render_schedule_config()


# ---------------------------------------------------------------------------
# Garmin Setup tab
# ---------------------------------------------------------------------------
def _update_config_env(updates: dict) -> None:
    """Update KEY=VALUE pairs in config.env and reload into os.environ."""
    env_path = config.config_env_path()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        lines = []
    existing_keys = {l.split("=", 1)[0] for l in lines if "=" in l}
    raw_lines_to_add = []
    for k, v in updates.items():
        if k == "GARMIN_PASSWORD":
            os.environ[k] = v
            raw_lines_to_add.append(f'{k}_RAW="{v}"')
            hashed, _ = config.hash_password(v)
            v = hashed
        if k in existing_keys:
            lines = [f'{k}="{v}"' if l.startswith(f"{k}=") else l for l in lines]
        else:
            lines.append(f'{k}="{v}"')
    for raw_line in raw_lines_to_add:
        raw_key = raw_line.split("=", 1)[0]
        if raw_key in existing_keys:
            lines = [raw_line if l.startswith(f"{raw_key}=") else l for l in lines]
        else:
            lines.append(raw_line)
    env_path.write_text("\n".join(lines) + "\n")
    for k, v in updates.items():
        if k != "GARMIN_PASSWORD":
            os.environ[k] = v


def _render_garmin_setup():
    """Render the Garmin Connect setup and sync tab."""
    from src.tasks.worker import BackgroundSync
    from src.ingestion.garmin_export import sync_routes_from_fit
    from src.db.store import CyclingDB

    st.subheader("Garmin Connect")

    current_email = os.environ.get("GARMIN_EMAIL", "")
    has_credentials = bool(current_email and os.environ.get("GARMIN_PASSWORD", ""))

    # ── Auth state machine ───────────────────────────────────────────
    # States: "idle" (no credentials), "mfa_pending" (needs OTP),
    # "connected" (authenticated), "error" (auth failed)
    auth_state = st.session_state.get("garmin_auth_state", "idle")
    auth_instance = st.session_state.get("garmin_auth_instance", None)
    auth_error = st.session_state.get("garmin_auth_error", "")

    # Determine effective connection status: connected if we have
    # credentials AND either a successful auth state or cached tokens
    # (use _check_garmin_connected so Settings tab matches Dashboard)
    has_cached_tokens = _check_garmin_connected()
    is_connected = auth_state == "connected" or (has_credentials and has_cached_tokens)

    # ── Auth status display ──────────────────────────────────────────
    if is_connected:
        col_status, col_action = st.columns([3, 1])
        with col_status:
            st.success(f"Connected as: {current_email}")
        with col_action:
            if st.button("Modify", key="modify_credentials"):
                st.session_state.show_clear_dialog = True
    elif auth_state == "mfa_pending":
        if auth_instance is None:
            # Session lost (e.g. browser restart) — need fresh login
            st.warning("MFA session expired. Please sign in again.")
            st.session_state.garmin_auth_state = "idle"
            st.rerun()
        else:
            st.warning("MFA required. Enter your verification code below.")
    elif auth_state == "error":
        st.error(f"Authentication failed: {auth_error}")
        if has_credentials:
            st.info("Your credentials are saved. Try signing in again.")
            if st.button("Sign In Again", key="signin_retry"):
                st.session_state.garmin_auth_state = "idle"
                st.session_state.garmin_auth_instance = None
                st.session_state.garmin_auth_error = ""
                st.rerun()
    elif has_credentials:
        # Credentials saved but not yet authenticated — try to auth now
        # (handles page reload after initial sign-in)
        st.info("Credentials saved. Attempting to authenticate...")
        from src.ingestion.garmin_connect import authenticate_garmin
        tokenstore = os.environ.get("GARMIN_TOKENSTORE", "")
        result, auth_inst = authenticate_garmin(
            current_email, os.environ.get("GARMIN_PASSWORD", ""), tokenstore
        )
        if result.success:
            st.session_state.garmin_auth_state = "connected"
            st.session_state.garmin_auth_instance = None
            st.session_state.garmin_auth_error = ""
            st.success(f"Connected as: {current_email}")
            is_connected = True
            st.rerun()
        elif result.mfa_required:
            st.session_state.garmin_auth_state = "mfa_pending"
            st.session_state.garmin_auth_instance = auth_inst
            st.session_state.garmin_auth_error = ""
            st.rerun()
        else:
            st.session_state.garmin_auth_state = "error"
            st.session_state.garmin_auth_error = result.error
            st.rerun()
    else:
        st.info("Not connected to Garmin Connect. Sign in below.")

    # ── Clear credentials dialog ─────────────────────────────────────
    if st.session_state.get("show_clear_dialog"):
        with st.form("clear_dialog", clear_on_submit=False):
            st.warning("This will clear your Garmin credentials. You'll need to sign in again.")
            c1, c2 = st.columns(2)
            with c1:
                yes_clicked = st.form_submit_button("Yes, Clear", type="primary")
            with c2:
                no_clicked = st.form_submit_button("No, Keep")
            if yes_clicked or no_clicked:
                st.session_state.show_clear_dialog = False
            if yes_clicked:
                _update_config_env({"GARMIN_EMAIL": "", "GARMIN_PASSWORD": ""})
                os.environ.pop("GARMIN_EMAIL", None)
                os.environ.pop("GARMIN_PASSWORD", None)
                st.session_state.garmin_auth_state = "idle"
                st.session_state.garmin_auth_instance = None
                st.session_state.garmin_auth_error = ""
                st.success("Credentials cleared.")
                st.rerun()
            if no_clicked:
                st.rerun()

    # ── MFA completion form ──────────────────────────────────────────
    if auth_state == "mfa_pending" and auth_instance is not None:
        with st.form("garmin_mfa", clear_on_submit=False):
            mfa_code = st.text_input(
                "Verification Code",
                type="password",
                placeholder="Enter the 6-digit code from your phone",
                key="garmin_mfa_input",
            )
            mfa_clicked = st.form_submit_button("Verify", type="primary")

            if mfa_clicked:
                if not mfa_code:
                    st.error("Verification code is required.")
                else:
                    from src.ingestion.garmin_connect import authenticate_garmin
                    result, auth_inst = authenticate_garmin(
                        "", "", "", mfa_code=mfa_code, auth_instance=auth_instance
                    )
                    if result.success:
                        st.session_state.garmin_auth_state = "connected"
                        st.session_state.garmin_auth_instance = None
                        st.session_state.garmin_auth_error = ""
                        st.success("Authenticated successfully!")
                        st.rerun()
                    else:
                        st.session_state.garmin_auth_error = result.error
                        st.error(f"Verification failed: {result.error}")
                        # Keep in mfa_pending so user can retry
                        st.session_state.garmin_auth_instance = auth_inst

    show_signin = (not has_credentials) or (auth_state == "error") or (auth_state == "mfa_pending" and auth_instance is None)
    # ── Sign-in form (when no credentials, in error state, or MFA session lost) ──
    if show_signin:
        with st.form("garmin_signin", clear_on_submit=False):
            email = st.text_input("Garmin Email", placeholder="you@garmin.com", key="garmin_email_input")
            password = st.text_input("Garmin Password", type="password", placeholder="Your Garmin Connect password", key="garmin_password_input")
            signin_clicked = st.form_submit_button("Sign In", type="primary")

            if signin_clicked:
                if not email or not password:
                    st.error("Email and password are required.")
                else:
                    _update_config_env({"GARMIN_EMAIL": email, "GARMIN_PASSWORD": password})
                    # Immediately attempt authentication
                    from src.ingestion.garmin_connect import authenticate_garmin
                    tokenstore = os.environ.get("GARMIN_TOKENSTORE", "")
                    result, auth_inst = authenticate_garmin(
                        email, password, tokenstore
                    )
                    if result.success:
                        st.session_state.garmin_auth_state = "connected"
                        st.session_state.garmin_auth_instance = None
                        st.session_state.garmin_auth_error = ""
                        st.success("Authenticated successfully!")
                        st.rerun()
                    elif result.mfa_required:
                        st.session_state.garmin_auth_state = "mfa_pending"
                        st.session_state.garmin_auth_instance = auth_inst
                        st.session_state.garmin_auth_error = ""
                        st.rerun()
                    else:
                        st.session_state.garmin_auth_state = "error"
                        st.session_state.garmin_auth_error = result.error
                        st.error(f"Authentication failed: {result.error}")

    # ── Sync state ───────────────────────────────────────────────────
    db_sync = CyclingDB(str(config.db_path("cycling_agent.sqlite")))
    last_wellness = db_sync.get_last_synced("garmin_wellness")
    last_activities = db_sync.get_last_synced("garmin_activities")
    wellness_count = db_sync.conn.execute("SELECT COUNT(*) FROM wellness").fetchone()[0]
    activities_count = db_sync.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    routes_count = db_sync.conn.execute(
        "SELECT COUNT(DISTINCT activity_id) FROM activity_routes"
    ).fetchone()[0]
    db_sync.close()

    col_w1, col_w2, col_w3 = st.columns(3)
    with col_w1:
        st.metric("Wellness Days", wellness_count)
    with col_w2:
        st.metric("Activities", activities_count)
    with col_w3:
        st.metric("Routes", routes_count)

    st.caption(
        f"Last wellness sync: {last_wellness or 'never'} · "
        f"Last activity sync: {last_activities or 'never'}"
    )

    # ── Historical sync ──────────────────────────────────────────────
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        st.caption("Re-sync all historical data from Garmin (may take a while).")
    with col2:
        sync_all_clicked = st.button(
            "Sync All Historical Data",
            disabled=not is_connected,
            key="sync_all_historical",
        )
    with col3:
        reparse_clicked = st.button(
            "🔁 Reparse FIT",
            use_container_width=True,
            help="Re-parse all local FIT files and recalculate metrics. No network calls.",
            key="reparse_fit",
        )
    if sync_all_clicked:
        if not st.session_state.get("syncing"):
            from src.tasks.worker import background_sync
            background_sync(
                days=3650,
                unbounded=True,
                run_analyze_after=True,
            )
            st.session_state.syncing = True
            st.session_state.sync_mode = "all"
            st.session_state.sync_days = 3650
            st.session_state.sync_result = None
            st.session_state.sync_error = None
            st.session_state.sync_log = []
            st.session_state.sync_progress = 0
            st.rerun()

    # ── Reparse FIT files ────────────────────────────────────────────
    if reparse_clicked:
        if not st.session_state.get("rearsing"):
            from src.tasks.worker import background_task
            from src.ingestion.garmin_connect import reparse_all_fit_files
            from src.main import run_analyze

            def _reparse_work(cb):
                cb(0, "Deleting existing stream data...")
                result = reparse_all_fit_files(
                    db_path=str(config.db_path("cycling_agent.sqlite")),
                    progress_callback=cb,
                )
                cb(90, "Running analytics...")
                run_analyze()
                cb(100, f"Done. {result['activities_processed']} activities, {result['stream_records']} records.")
                return result

            background_task(_reparse_work, result_key="reparse")
            st.session_state.rearsing = True
            st.session_state.sync_log = []
            st.session_state.sync_progress = 0
            st.rerun()

    # ── Render progress dialog ───────────────────────────────────────
    _render_sync_progress()

    # ── Show sync errors ──────────────────────────────────────────────
    if st.session_state.get("sync_error"):
        err = st.session_state.sync_error
        st.error(f"Sync failed: {err}")
        st.session_state.sync_error = None

    # ── Show sync results ─────────────────────────────────────────────
    if st.session_state.get("sync_result"):
        result = st.session_state.sync_result
        with st.expander("Sync Results", expanded=True):
            _display_sync_results(result)
            if st.button("Done", type="primary", use_container_width=True, key="dismiss_sync_results"):
                st.session_state.sync_result = None
                st.session_state.sync_log = []
                st.session_state.sync_progress = 0
                st.rerun()

# ---------------------------------------------------------------------------
# Garmin connection check (session state or cached tokens)
# ---------------------------------------------------------------------------
def _check_garmin_connected() -> bool:
    """Return True if Garmin is connected via session state or has cached tokens."""
    if st.session_state.get("garmin_auth_state") == "connected":
        return True
    # Check for cached tokens on disk
    tokenstore = os.environ.get("GARMIN_TOKENSTORE", "")
    if tokenstore and os.path.isdir(tokenstore):
        files = [f for f in os.listdir(tokenstore) if f.endswith(('.json', '.pkl'))]
        if files:
            return True
    return False


def _display_sync_results(result: dict) -> None:
    """Render formatted sync results."""
    # Wellness summary
    if "wellness" in result:
        w = result["wellness"]
        recs = w.get("wellness_records", 0)
        hrv = w.get("with_hrv", 0)
        st.write(f"**Wellness:** {recs} record(s) synced{f' ({hrv} with HRV)' if hrv else ''}")

    # Activities summary
    if "activities" in result:
        a = result["activities"]
        processed = a.get("activities_processed", 0)
        streams = a.get("stream_records", 0)
        st.write(f"**Activities:** {processed} processed, {streams} stream records")

    # Analysis summary
    if "analysis" in result:
        analysis = result["analysis"]
        cp = analysis.get("cp")
        readiness = analysis.get("readiness")
        training_load = analysis.get("training_load")

        # Metrics row
        cols = st.columns(4)
        if cp:
            cols[0].metric("Critical Power", f"{cp:.0f} W")
        if training_load:
            cols[1].metric("CTL", f"{training_load.get('ctl', 0):.0f}")
            cols[2].metric("ATL", f"{training_load.get('atl', 0):.0f}")
            cols[3].metric("TSB", f"{training_load.get('tsb', 0):.0f}")

        # Readiness breakdown
        if readiness:
            st.markdown("---")
            st.write(f"**Readiness:** {readiness.get('state', 'unknown').replace('_', ' ').title()}")
            score = readiness.get("composite_score", 0)
            state = readiness.get("state", "unknown")

            # Color-coded score
            if score >= 70:
                emoji = "🟢"
            elif score >= 50:
                emoji = "🟡"
            else:
                emoji = "🔴"
            st.write(f"{emoji} **Score:** {score:.0f}/100")

            # Key metrics
            r_cols = st.columns(3)
            r_cols[0].write(f"**HRV (RMSSD):** {readiness.get('rmssd', '—')}")
            r_cols[1].write(f"**RHR:** {readiness.get('resting_hr', '—')}")
            r_cols[2].write(f"**Limiting Factor:** {readiness.get('limiting_factor', '—')}")

            rec = readiness.get("recommendation", "")
            if rec:
                st.info(rec)

    # Prescription
    if "prescription" in result:
        st.markdown("---")
        st.subheader("Today's Prescription")
        st.markdown(result["prescription"])
    if "prescription_error" in result:
        st.error(f"Prescription failed: {result['prescription_error']}")

    # Reparse
    if "reparse" in result:
        rp = result["reparse"]
        processed = rp.get("activities_processed", 0)
        records = rp.get("stream_records", 0)
        st.write(f"**Re-parsed:** {processed} activities, {records} stream records")

def _generate_readiness_explanation(analyze_result: dict) -> str:
    """Generate a plain-English readiness explanation from data (no LLM)."""
    readiness = analyze_result.get("readiness", {})
    training_load = analyze_result.get("training_load", {})
    cp = analyze_result.get("cp")

    if not readiness:
        return ""

    state = readiness.get("state", "unknown")
    score = readiness.get("composite_score", 0)
    rmssd = readiness.get("rmssd")
    rmssd_mean = readiness.get("rmssd_mean")
    rhr = readiness.get("resting_hr")
    rhr_mean = readiness.get("rhr_mean")
    limiting = readiness.get("limiting_factor", "")
    ctl = training_load.get("ctl", 0)
    atl = training_load.get("atl", 0)
    tsb = training_load.get("tsb", 0)

    parts = []

    # State description
    state_desc = {
        "parasympathetic_hyperactivity": "Your nervous system is in a deep recovery mode. This usually means you've accumulated significant fatigue and your body is pushing back against more load. It's a sign to take it easy.",
        "parasympathetic_dominance": "Your body is in a strong recovery state. The parasympathetic nervous system is active, meaning you're well-rested and ready for a harder session.",
        "sympathetic_dominance": "Your sympathetic nervous system is elevated — you're in a 'fight or flight' state. This can come from stress, illness, or overreaching. Keep intensity low.",
        "sympathetic_stress": "You're showing signs of sympathetic stress. Your body is under pressure from training or life stress. Consider reducing volume or intensity today.",
        "eustress": "You're in a healthy stress zone — enough stimulus to adapt without being overwhelmed. A good day for a planned training session.",
        "unknown": "Not enough data to determine readiness state yet.",
    }
    parts.append(state_desc.get(state, f"Readiness state is {state.replace('_', ' ')}."))

    # HRV analysis
    if rmssd is not None and rmssd_mean is not None:
        if rmssd < rmssd_mean - 5:
            parts.append(f"Your HRV ({rmssd:.0f}) is below your 7-day average ({rmssd_mean:.0f}), suggesting your body hasn't fully recovered. This often shows up after hard training blocks, poor sleep, or illness.")
        elif rmssd > rmssd_mean + 5:
            parts.append(f"Your HRV ({rmssd:.0f}) is above your 7-day average ({rmssd_mean:.0f}), which is a positive sign of good recovery.")
        else:
            parts.append(f"Your HRV ({rmssd:.0f}) is close to your 7-day average ({rmssd_mean:.0f}) — nothing unusual here.")

    # RHR analysis
    if rhr is not None and rhr_mean is not None:
        if rhr > rhr_mean + 5:
            parts.append(f"Your resting heart rate ({rhr:.0f}) is above your 7-day average ({rhr_mean:.0f}), which can indicate incomplete recovery, dehydration, or illness.")
        elif rhr < rhr_mean - 5:
            parts.append(f"Your resting heart rate ({rhr:.0f}) is below your 7-day average ({rhr_mean:.0f}) — a good sign of recovery.")

    # Limiting factor
    if limiting:
        factor_desc = {
            "stress": "Life stress is the main drag on your readiness right now.",
            "load": "Recent training load is the limiting factor. Your body needs more time to adapt.",
            "rmssd": "Low HRV is holding your readiness score down.",
            "rhr": "Elevated resting heart rate is the main concern.",
        }
        parts.append(f"The main limiting factor is {limiting}: {factor_desc.get(limiting, '')}")

    # Training load context
    if ctl and atl:
        if ctl > 100:
            load_note = "Your chronic training load (CTL) is high, meaning you've built a solid fitness base."
        elif ctl > 50:
            load_note = f"Your chronic training load (CTL {ctl:.0f}) is moderate — you're building fitness steadily."
        else:
            load_note = f"Your chronic training load (CTL {ctl:.0f}) is low — there's room to build volume."
        parts.append(load_note)

        if tsb < -20:
            parts.append("Your training stress balance is deeply negative — you're in a fatigue phase. Recovery sessions only.")
        elif tsb < 0:
            parts.append("Your training stress balance is slightly negative — you're accumulating some fatigue. Keep today moderate.")
        elif tsb > 20:
            parts.append("Your training stress balance is positive — you're in a recovery phase. Good day for a harder effort.")

    # Recommendation
    if score >= 70:
        parts.append("Overall: you're in good shape to train today as planned.")
    elif score >= 50:
        parts.append("Overall: keep today moderate. If you feel off, err on the side of lighter effort.")
    else:
        parts.append("Overall: today should be easy — recovery ride, walk, or rest. Don't push it.")

    return " ".join(parts)


def _save_readiness_explanation(explanation: str, analyze_result: dict) -> None:
    """Save readiness explanation to latest_analysis.json."""
    try:
        from src import config as cfg
        import json

        result_path = cfg.vault_path() / "data" / "latest_analysis.json"
        data = {}
        if result_path.exists():
            with open(result_path) as f:
                data = json.load(f)
        data["readiness_explanation"] = explanation
        data["cp"] = analyze_result.get("cp")
        data["readiness"] = analyze_result.get("readiness")
        data["training_load"] = analyze_result.get("training_load")
        with open(result_path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
# ---------------------------------------------------------------------------
# LLM Settings
# ---------------------------------------------------------------------------
def _render_llm_settings():
    """Render OpenAI-compatible LLM endpoint configuration."""
    from src.agent.llm_client import _discover_models
    from src.config.llm_config import load_llm_config, save_llm_config

    st.subheader("LLM Endpoint")
    st.caption("Configure your OpenAI-compatible API endpoint (vLLM, Ollama, LM Studio, etc.).")

    cfg = load_llm_config()

    base_url = st.text_input(
        "Base URL",
        value=cfg.get("base_url", ""),
        key="llm_base_url",
        help="OpenAI-compatible /v1 endpoint (e.g. http://192.168.1.119:8013/v1)",
    )
    api_key = st.text_input(
        "API Key",
        value=cfg.get("api_key", ""),
        type="password",
        key="llm_api_key",
        help="API key (optional for local servers)",
    )
    timeout = st.number_input(
        "Timeout (seconds)",
        min_value=10,
        max_value=600,
        value=int(cfg.get("timeout", 120)),
        key="llm_timeout",
        help="Request timeout in seconds",
    )

    if st.button("🔍 Test Connection", type="primary", use_container_width=True, key="test_llm"):
        import requests
        test_url = base_url.rstrip("/") + "/models"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            resp = requests.get(test_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                models = [m.get("id", "") for m in data.get("data", [])]
                if models:
                    st.session_state.llm_models = models
                    st.session_state.llm_test_ok = True
                    st.rerun()
                else:
                    st.error("Connected but no models returned. Check your endpoint config.")
            else:
                st.error(f"Server returned HTTP {resp.status_code}: {resp.text[:200]}")
        except requests.exceptions.ConnectionError:
            st.error(f"Cannot connect to {test_url}. Is the server running and reachable from the container?")
        except Exception as e:
            st.error(f"Connection failed: {e}")

    if st.session_state.get("llm_test_ok"):
        st.success(f"Connected! {len(st.session_state.llm_models)} model(s) found.")
        st.session_state.llm_test_ok = False

    if st.session_state.get("llm_models"):
        models = st.session_state.llm_models
        st.caption("Select a model from the discovered list:")
        selected = st.selectbox(
            "Available Models",
            models,
            index=0 if not cfg.get("model") else (models.index(cfg["model"]) if cfg["model"] in models else 0),
            key="llm_model_select",
        )
        if selected:
            cfg["model"] = selected
            save_llm_config(cfg)
            st.success(f"Model set to **{selected}**")

    st.divider()
    if st.button("💾 Save", type="primary", use_container_width=True, key="save_llm"):
        save_llm_config({
            "base_url": base_url,
            "api_key": api_key,
            "model": cfg.get("model", ""),
            "timeout": timeout,
        })
        st.session_state.llm_saved = True

    if st.session_state.get("llm_saved"):
        st.success("Saved!")
        st.session_state.llm_saved = False

# ---------------------------------------------------------------------------
# Memory Journal Settings
# ---------------------------------------------------------------------------
def _render_memory_settings():
    """Render memory journal viewer and management controls."""
    from src.memory.journal import load_journal, append_entry
    from pathlib import Path
    from src.config import vault_path

    st.subheader("Memory Journal")
    st.caption("Facts your AI coach remembers across sessions. Edit the file directly or use controls below.")

    journal_text = load_journal()

    # Scrollable journal viewer
    st.markdown(
        '<div style="max-height: 300px; overflow-y: auto; border: 1px solid #ddd; '
        'border-radius: 4px; padding: 12px; background: #fafafa; font-family: monospace; '
        'font-size: 0.85em; white-space: pre-wrap;">'
        + (journal_text if journal_text else "<em>No entries yet. Chat with the coach to build memory.</em>")
        + "</div>",
        unsafe_allow_html=True,
    )

    col1, col2, col3 = st.columns([1, 1, 3])

    with col1:
        if st.button("🗑️ Clear Journal", use_container_width=True, key="clear_journal"):
            journal_path = vault_path() / "memory_journal.md"
            if journal_path.exists():
                journal_path.write_text("", encoding="utf-8")
            st.rerun()

    with col2:
        if st.button("📝 Add Entry", use_container_width=True, key="add_entry_btn"):
            st.session_state.show_manual_entry = True

    # Manual entry form
    if st.session_state.get("show_manual_entry"):
        st.markdown("---")
        entry_text = st.text_area(
            "New Journal Entry",
            key="manual_journal_entry",
            placeholder="e.g., Left knee hurts after long rides",
            height=80,
        )
        entry_col1, entry_col2 = st.columns(2)
        with entry_col1:
            if st.button("💾 Save Entry", type="primary", use_container_width=True, key="save_manual_entry"):
                if entry_text.strip():
                    append_entry(entry_text.strip())
                    st.session_state.show_manual_entry = False
                    st.rerun()
        with entry_col2:
            if st.button("Cancel", use_container_width=True, key="cancel_manual_entry"):
                st.session_state.show_manual_entry = False
                st.rerun()

# ---------------------------------------------------------------------------
# Shared sync controls (used by Coach page)
# ---------------------------------------------------------------------------
def _render_sync_controls():
    """Render sync/prescribe buttons, progress, and results."""
    is_connected = _check_garmin_connected()

    # ── Buttons ───────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        sync_clicked = st.button(
            "Update Latest Data", type="primary",
            disabled=not is_connected,
            help="Pull new activities and wellness from Garmin, then re-run analytics.",
            key="sync_since_last",
        )
    with col2:
        prescribe_clicked = st.button(
            "Generate Prescription",
            help="Run analytics and generate a training prescription via LLM.",
            key="generate_prescription",
        )

    # ── Handle button clicks ──────────────────────────────────────────
    if sync_clicked:
        if not st.session_state.get("syncing"):
            from src.tasks.worker import background_sync
            background_sync(
                days=7,
                unbounded=False,
                run_analyze_after=True,
            )
            st.session_state.syncing = True
            st.session_state.sync_mode = "update"
            st.session_state.sync_days = 7
            st.session_state.sync_result = None
            st.session_state.sync_error = None
            st.session_state.sync_log = []
            st.session_state.sync_progress = 0
            st.rerun()

    if prescribe_clicked:
        if not st.session_state.get("syncing"):
            from src.tasks.worker import background_task
            from src.main import run_analyze, run_prescribe

            def _prescribe_work(cb):
                cb(10, "Running analysis...")
                analyze_result = run_analyze()
                cb(50, "Generating prescription...")
                prescription = run_prescribe(analyze_result)
                cb(90, "Prescription generated.")
                cb(100, "Prescription complete.")
                return {
                    "analysis": {
                        "cp": analyze_result.get("cp"),
                        "readiness": analyze_result.get("readiness"),
                        "training_load": analyze_result.get("training_load"),
                    },
                    "prescription": prescription,
                }

            background_task(_prescribe_work, result_key="prescribe")
            st.session_state.syncing = True
            st.session_state.sync_mode = "prescribe"
            st.session_state.sync_result = None
            st.session_state.sync_error = None
            st.session_state.sync_log = []
            st.session_state.sync_progress = 0
            st.rerun()

    # ── Render progress dialog ───────────────────────────────────────
    _render_sync_progress()

    # ── Show sync errors ──────────────────────────────────────────────
    if st.session_state.get("sync_error"):
        err = st.session_state.sync_error
        st.error(f"Sync failed: {err}")
        if "MFA" in err or "mfa" in err or "two-factor" in err:
            st.info("Your session has expired. Please sign in again.")
        st.session_state.sync_error = None



# ---------------------------------------------------------------------------
# Coach Page — AI-powered chat with cycling context
# ---------------------------------------------------------------------------
def _render_coach():
    """Render the AI Coach chat page.

    Loads the latest analysis and lets the user chat with an LLM
    that has full context about their cycling data.
    """
    st.header("AI Coach")
    st.caption("Chat with your AI coach about training, recovery, and performance.")

    # Sync/prescribe controls at top
    _render_sync_controls()
    st.divider()

    # Initialize chat history
    if "coach_messages" not in st.session_state:
        st.session_state.coach_messages = []

    # Load latest analysis for context
    analysis = None
    from src import config as cfg
    result_path = cfg.vault_path() / "data" / "latest_analysis.json"
    if result_path.exists():
        try:
            import json
            with open(result_path) as f:
                analysis = json.load(f)
        except Exception:
            pass

    # Show persistent status card
    if analysis:
        readiness = analysis.get("readiness", {})
        training_load = analysis.get("training_load", {})
        cp = analysis.get("cp")

        state = readiness.get("state", "unknown").replace("_", " ").title()
        score = readiness.get("composite_score", 0)
        if score >= 70:
            color = "#4caf50"
        elif score >= 50:
            color = "#ff9800"
        else:
            color = "#f44336"

        # Build status card
        st.markdown(f"""
        <div style="background:#{color}15; border-left: 4px solid {color};
                     padding: 16px 20px; border-radius: 4px; margin-bottom: 8px;">
            <div style="font-size: 1.1em; font-weight: 600; color: {color};">
                {state} — Readiness {score:.0f}/100
            </div>
        </div>
        """, unsafe_allow_html=True)

        # Key metrics row
        m_cols = st.columns(6)
        m_cols[0].metric("CP", f"{cp:.0f}W" if cp else "—")
        m_cols[1].metric("CTL", f"{training_load.get('ctl', 0):.0f}")
        m_cols[2].metric("ATL", f"{training_load.get('atl', 0):.0f}")
        m_cols[3].metric("TSB", f"{training_load.get('tsb', 0):.0f}")
        m_cols[4].metric("HRV", f"{readiness.get('rmssd', '—')}")
        m_cols[5].metric("RHR", f"{readiness.get('resting_hr', '—')}")

        # Detailed explanation
        explanation = analysis.get("readiness_explanation")
        if not explanation and readiness:
            explanation = _generate_readiness_explanation(analysis)
            _save_readiness_explanation(explanation, analysis)

        if explanation:
            st.markdown(explanation)
        else:
            rec = readiness.get("recommendation", "")
            if rec:
                st.info(rec)
    else:
        st.info("No analysis data yet. Use 'Update Latest Data' above to sync and analyze.")

    # Display chat history
    chat_container = st.container()
    with chat_container:
        for msg in st.session_state.coach_messages:
            role = msg["role"]
            content = msg["content"]
            if role == "user":
                st.markdown(f"**You:** {content}")
            elif role == "assistant":
                st.markdown(f"**Coach:** {content}")
            st.divider()

    # Chat input
    user_input = st.text_input(
        "Ask your coach...",
        key="coach_input",
        placeholder="e.g., Should I train today? How's my recovery?",
        label_visibility="collapsed",
    )

    col_btn, col_clear = st.columns([1, 1])
    with col_btn:
        send_clicked = st.button("Send", type="primary", use_container_width=True, key="coach_send")
    with col_clear:
        clear_clicked = st.button("Clear Chat", use_container_width=True, key="coach_clear")

    if clear_clicked:
        st.session_state.coach_messages = []
        st.rerun()

    if send_clicked and user_input.strip():
        # Add user message
        st.session_state.coach_messages.append({"role": "user", "content": user_input.strip()})

        # Build system prompt with context
        from src.agent import prompt_builder, llm_client
        from src.memory.journal import load_recent, extract_memories, append_entry

        system_prompt = prompt_builder.build_system_prompt(
            readiness=analysis.get("readiness") if analysis else None,
            recent_activities=analysis.get("recent_activities") if analysis else None,
            thresholds=analysis.get("thresholds") if analysis else None,
            w_prime=analysis.get("w_prime") if analysis else None,
            durability=analysis.get("durability") if analysis else None,
            decoupling=analysis.get("decoupling") if analysis else None,
        )

        # Prepend recent memory journal entries to system prompt
        journal_context = load_recent(30)
        if journal_context:
            system_prompt = f"## Memory Journal\n{journal_context}\n\n{system_prompt}"

        # Build conversation context
        messages = [{"role": "system", "content": system_prompt}]
        for msg in st.session_state.coach_messages:
            messages.append({"role": msg["role"], "content": msg["content"]})

        # Show loading
        with st.spinner("Coach is thinking..."):
            try:
                # Build a single prompt with conversation history
                conv_text = "\n".join(
                    f"{m['role'].upper()}: {m['content']}"
                    for m in st.session_state.coach_messages
                )
                full_prompt = f"{system_prompt}\n\nConversation:\n{conv_text}\n\nASSISTANT:"

                response = llm_client.generate(full_prompt, stream=False)
                st.session_state.coach_messages.append({"role": "assistant", "content": response})

                # Extract memories in background thread (non-blocking)
                def _extract_and_save():
                    try:
                        bullets = extract_memories(user_input.strip(), response)
                        for bullet in bullets:
                            append_entry(bullet)
                    except Exception:
                        pass  # Silently skip if extraction fails

                threading.Thread(target=_extract_and_save, daemon=True).start()
            except Exception as e:
                st.error(f"Coach error: {e}")
                st.info("Check that your LLM server is running (LLM_BASE_URL env var).")

        # Clear input and rerun to show response
        st.session_state.coach_input_value = ""
        st.rerun()
# ---------------------------------------------------------------------------
# Weekly Calendar Page
# ---------------------------------------------------------------------------
def _render_weekly_calendar():
    """Render the 7-day training calendar."""
    from src.analytics.weekly_planner import generate_weekly_plan, save_weekly_plan, load_weekly_plan
    from src.config.schedule import load_schedule, save_schedule, DAY_NAMES

    st.header("Weekly Training Plan")

    # Load or generate plan
    plan = load_weekly_plan()

    # Two generate buttons: Rules (instant) and AI (LLM)
    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        if plan:
            week_label = f"Plan from {plan.week_start}"
            st.caption(week_label)
        else:
            st.caption("No plan generated yet")
    with col2:
        if st.button("📊 Rules", type="primary", use_container_width=True, key="generate_rules"):
            try:
                plan = generate_weekly_plan()
                save_weekly_plan(plan)
                st.session_state.week_generated = True
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")
    with col3:
        if st.button("🤖 AI", use_container_width=True, key="generate_ai"):
            try:
                from src.analytics.weekly_planner import generate_ai_plan
                plan = generate_ai_plan()
                save_weekly_plan(plan)
                st.session_state.week_generated = True
                st.rerun()
            except Exception as e:
                st.error(f"AI generation failed: {e}")

    if st.session_state.get("week_generated"):
        st.success("Weekly plan generated!")
        st.session_state.week_generated = False

    if not plan:
        st.info("Generate a plan: **Rules** uses readiness + weather. **AI** adds LLM analysis of your history.")
        _render_schedule_config()
        return

    # Summary row
    st.markdown(f"`{plan.readiness_summary}`")
    s_cols = st.columns(3)
    s_cols[0].metric("Weekly TSS Target", f"{plan.weekly_tss_target:.0f}")
    s_cols[1].metric("Weekly TSS Planned", f"{plan.weekly_tss_planned:.0f}")
    train_days = sum(1 for d in plan.days if not d.rest_day)
    s_cols[2].metric("Training Days", str(train_days))

    st.divider()

    # Calendar grid
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Intensity colors
    zone_colors = {
        "rest": "#666",
        "recovery": "#4caf50",
        "endurance": "#2196f3",
        "threshold": "#ff9800",
        "vo2": "#f44336",
        "anaerobic": "#9c27b0",
        "mixed": "#00bcd4",
    }

    for i, day in enumerate(plan.days):
        day_name = day_labels[day.weekday]
        day_date = day.date.split("-")[2]

        color = zone_colors.get(day.session_type, "#666")
        is_today = day.date == date.today().isoformat()
        border = "2px solid #fff" if is_today else "1px solid #333"
        bg = "#1e1e2e" if is_today else "#0f1117"
        color_display = color if not is_today else "#fff"

        # Weather icon
        weather_icons = {"clear": "☀️", "cloudy": "⛅", "rain": "🌧", "snow": "❄️", "storm": "⛈"}
        w_icon = weather_icons.get(day.weather_condition, "")
        tmax_f = day.weather_temp_max if day.weather_temp_max else 0
        tmin_f = day.weather_temp_min if day.weather_temp_min else 0
        w_temp = f"{tmax_f:.0f}°F/{tmin_f:.0f}°F" if tmax_f else ""
        w_line = f"{w_icon} {w_temp}" if w_temp else ""

        if day.rest_day:
            ride_line = f'<div style="color: #555; font-size: 0.75em; margin-top: 2px;">{day.ride_note}</div>' if day.ride_note else ''
            content = f"""
            <div style="padding: 12px; border: {border}; border-radius: 8px; background: {bg}; text-align: center; min-height: 90px;">
                <div style="font-weight: 600; color: #888;">{day_name} {day_date}</div>
                <div style="color: #666; margin-top: 8px;">Rest</div>
                <div style="color: #555; font-size: 0.8em; margin-top: 4px;">{w_line}</div>
                {ride_line}
            </div>"""
        else:
            indoor_icon = "🏠" if day.indoor else "🚴"
            ride_line = f'<div style="color: #555; font-size: 0.75em; margin-top: 2px;">{day.ride_note}</div>' if day.ride_note else ''
            content = f"""
            <div style="padding: 12px; border: {border}; border-radius: 8px; background: {bg}; text-align: center; min-height: 110px;">
                <div style="font-weight: 600; color: {color_display};">{day_name} {day_date}</div>
                <div style="font-size: 1.2em; margin: 4px 0;">{indoor_icon} {day.session_type.title()}</div>
                <div style="color: #aaa; font-size: 0.85em;">{day.duration_min}min · TSS {day.target_tss:.0f}</div>
                <div style="color: {color_display}; font-size: 0.8em;">{day.target_zone}</div>
                <div style="color: #555; font-size: 0.8em; margin-top: 4px;">{w_line}</div>
                {ride_line}
            </div>"""

        st.markdown(content, unsafe_allow_html=True)

        # Workout description always visible
        if not day.rest_day and day.description:
            st.caption(day.description)
        if day.ride_note:
            st.caption(f"🚴 {day.ride_note}")
        if day.weather_note:
            st.caption(f"🌤 {day.weather_note}")

    st.divider()

    # Projected fitness/fatigue/load chart
    if plan and plan.ctl_series:
        import plotly.graph_objects as go

        labels = [f"{d.date.split('-')[1]}/{d.date.split('-')[2]}" for d in plan.days]

        fig = go.Figure()
        fig.add_trace(go.Scatter(x=labels, y=plan.ctl_series, name="CTL (Fitness)",
                                  line=dict(color="#2196f3", width=2), mode="lines+markers"))
        fig.add_trace(go.Scatter(x=labels, y=plan.atl_series, name="ATL (Fatigue)",
                                  line=dict(color="#f44336", width=2), mode="lines+markers"))
        fig.add_trace(go.Scatter(x=labels, y=plan.tsb_series, name="TSB (Form)",
                                  line=dict(color="#4caf50", width=2), mode="lines+markers"))

        fig.update_layout(
            height=200, margin=dict(l=50, r=20, t=10, b=30),
            xaxis_title="", yaxis_title="",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(size=11),
        )
        fig.update_xaxes(showgrid=False)
        fig.update_yaxes(showgrid=True, gridcolor="#333")
        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # Schedule config
    _render_schedule_config()


def _render_schedule_config():
    """Render 7x24 hour availability grid using table rows for alignment."""
    from src.config.schedule import load_schedule, save_schedule, DAY_NAMES

    schedule = load_schedule()
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    with st.expander("⚙️ Training Schedule", expanded=True):
        st.caption("Click cells to toggle availability. Each row = 1 hour block.")

        # Quick presets
        p1, p2, p3, p4 = st.columns(4)
        presets = {
            "p_morning": ("Morning", [6,7,8,9,10,11]),
            "p_afternoon": ("Afternoon", [12,13,14,15,16,17]),
            "p_evening": ("Evening", [18,19,20,21]),
            "p_all": ("All Day", list(range(6, 22))),
        }
        for col, (label, hours) in zip([p1, p2, p3, p4], presets.values()):
            if col.button(label, key=f"preset_{label}"):
                for day in DAY_NAMES:
                    schedule[day] = {"available_hours": list(hours)}
                st.rerun()

        st.divider()

        # Header row
        hdr_cols = st.columns([0.8, 1, 1, 1, 1, 1, 1, 1])
        hdr_cols[0].markdown("**Time**")
        for i, label in enumerate(day_labels):
            hdr_cols[i + 1].markdown(f"**{label}**")

        # One row per hour — use st.columns with consistent sizing
        for hour in range(24):
            row_cols = st.columns([0.8, 1, 1, 1, 1, 1, 1, 1])
            # Use empty spacer + checkbox-style label to match vertical position
            row_cols[0].markdown(f'<div style="display:flex;align-items:center;justify-content:center;height:24px;"><span style="font-size:11px;color:#888;font-family:monospace;">{hour:02d}:00</span></div>', unsafe_allow_html=True)
            for day_idx, day_name in enumerate(DAY_NAMES):
                hours = set(schedule.get(day_name, {}).get("available_hours", []))
                checked = hour in hours
                if row_cols[day_idx + 1].checkbox(
                    "", value=checked, key=f"hour_{day_name}_{hour}",
                    label_visibility="collapsed",
                ):
                    hours.add(hour)
                else:
                    hours.discard(hour)
                schedule[day_name] = {"available_hours": sorted(hours)}

        if st.button("Save Schedule", type="primary", use_container_width=True, key="save_schedule"):
            save_schedule(schedule)
            st.session_state.schedule_saved = True

        if st.session_state.get("schedule_saved"):
            st.success("Schedule saved!")
            st.session_state.schedule_saved = False
    st.divider()
# ---------------------------------------------------------------------------
# Main dispatch
# ---------------------------------------------------------------------------
if nav_page == "Dashboard":
    _render_dashboard()
elif nav_page == "Activity Detail":
    _render_activity_detail()
elif nav_page == "Trends":
    _render_trends()
elif nav_page == "Map":
    _render_map()
elif nav_page == "Profile":
    _render_profile()
elif nav_page == "Settings":
    _render_garmin_setup()
    st.divider()
    _render_llm_settings()
    st.divider()
    _render_memory_settings()
