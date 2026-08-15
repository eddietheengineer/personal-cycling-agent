# Review: `src/visualize.py`

3,271 lines. The entire Streamlit dashboard: 7 pages (Dashboard, Activities, Trends, Map, Profile, Wiki, Settings), sync progress, coach chat, Garmin auth, LLM settings, memory journal, wiki UI.

## Findings

### 1. The file is 3,271 lines — it should be 7 files

Each page is a `_render_*` function in the same file. The module-level code (lines 322-398) runs on every import: `st.set_page_config`, DB connection, scheduler start, wiki seeding, sidebar nav. **Change:** split into `pages/dashboard.py`, `pages/activities.py`, etc. (Streamlit multi-page app) or at minimum extract each `_render_*` into its own module.

### 2. `latest_analysis.json` is read on every Dashboard render (lines 619-627, 899-904)

`_render_readiness_card` and `_render_dashboard_coach` both open and parse `latest_analysis.json` on every rerun. For a large analysis dict (14 activities × 10 metrics), this is wasteful. **Change:** cache in `st.session_state` with a file-mtime check.

### 3. `_save_readiness_explanation` overwrites `latest_analysis.json` with a 3-key subset (lines 2516-2534)

```python
data["readiness_explanation"] = explanation
data["cp"] = analyze_result.get("cp")
data["readiness"] = analyze_result.get("readiness")
data["training_load"] = analyze_result.get("training_load")
```
It reads the existing file, adds 4 keys, and writes back. But if the file was written by `run_analyze` (which has ~15 keys), this preserves them. However, if the file was deleted or corrupted, it creates a new file with only 4 keys — the Dashboard's readiness card would show "No analysis data" for everything else. **Change:** don't write to the analysis file from the UI; store the explanation in session state or a separate file.

### 4. `_render_garmin_setup` opens 3 separate DB connections (lines 2020, 2274, 2094)

`db_sync = CyclingDB(...)` (line 2020), `db_pm = CyclingDB(...)` (line 2274), and another `db = CyclingDB(...)` (line 2094, force resync). Each opens a new SQLite connection. The session-state `db` (line 342) is available but not used here. **Change:** use the session-state `db` for all queries.

### 5. `_update_config_env` stores the raw password alongside the hash (lines 1829-1833)

```python
if k == "GARMIN_PASSWORD":
    os.environ[k] = v
    raw_lines_to_add.append(f'{k}_RAW="{v}"')
    hashed, _ = config.hash_password(v)
    v = hashed
```
The plaintext password is written to `config.env` as `GARMIN_PASSWORD_RAW="..."` while the hash is stored as `GARMIN_PASSWORD="..."`. This is the same finding as `01-config.md` — the "hash" is decorative because the plaintext is right next to it. **Change:** store only the hash; use a keyring or encrypted store for the plaintext if re-authentication is needed.

### 6. `_check_garmin_connected` checks for token files on disk (lines 2331-2353)

It lists the tokenstore directory and returns True if any `.json` or `.pkl` file exists. This is a weak check — a stale/corrupt token file would report "connected" but the actual auth would fail. **Change:** attempt a lightweight token validation (e.g. check the token's expiry timestamp) or just try a cached login.

### 7. Coach chat: the `QUERY:` loop has no iteration limit (lines 944-954)

```python
if response.strip().startswith("QUERY:"):
    sql = response.strip()[6:].strip()
    result = query_db(sql, cfg.vault_path())
    followup = f"{full_prompt}\n\nASSISTANT: QUERY: {sql}\nSYSTEM: Query results:\n{result}\n\nASSISTANT:"
    response = llm_client.generate(followup, stream=False)
```
If the LLM responds with another `QUERY:` in the followup, the code doesn't loop — it just uses the second response as-is. But if the second response *also* starts with `QUERY:`, it's shown to the user as a raw SQL query. **Change:** add a max-iterations guard (e.g. 2 queries max) and strip any remaining `QUERY:` prefix from the final response.

### 8. Coach chat: `full_prompt` includes the entire conversation history (lines 930-934)

```python
conv_text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in st.session_state.coach_messages)
full_prompt = f"{system_prompt}\n\nConversation:\n{conv_text}\n\nASSISTANT:"
```
The conversation grows unbounded in session state. After 50 messages, the prompt is huge (system prompt + 50 messages + analytics). The `_split_prompt_into_messages` parser in `llm_client.py` then has to parse all of it. **Change:** truncate the conversation to the last N messages (e.g. 10) before building the prompt.

### 9. `_render_week_strip` fetches weather on every render (lines 530-536)

Already flagged in `10-weather-journal.md` finding 2. The Dashboard calls `get_weekly_forecast` (blocking HTTP) on every rerun. **Change:** cache with `st.cache_data(ttl=3600)`.

### 10. `_render_trends` calls `db.get_activity_metrics_by_date()` twice (lines 1309, 1322)

```python
all_metrics_for_cp = db.get_activity_metrics_by_date()  # line 1309
...
all_metrics = db.get_activity_metrics_by_date()  # line 1322
```
Same query, same result, called twice. **Change:** call once and reuse.

### 11. `_render_map` computes haversine in Python for every activity (lines 1571-1579)

For each centroid, it computes the haversine distance in a Python loop. For 1,000 activities this is fine, but it could be done in SQL (SQLite has no built-in haversine, but a bounding-box pre-filter would reduce the Python work). **Change:** add a bounding-box SQL filter before the Python haversine.

### 12. `_render_profile` reads env vars as initial values (lines 1645-1664)

`profile = {"name": os.getenv("ATHLETE_NAME", ""), "weight_kg": int(os.getenv("WEIGHT_KG", "0")), ...}` — the env vars are the *initial* values, then `_parse_profile_text` overwrites them from the markdown file. But if the markdown file doesn't exist, the profile is populated from env vars that may be stale. **Change:** read from the markdown file only; use env vars as a fallback for first-run only.

### 13. `_render_schedule_config` creates 24×7 = 168 checkboxes (lines 3227-3241)

Each checkbox is a Streamlit widget with its own key. 168 widgets in a single render is heavy. **Change:** use a single `st.data_editor` or a custom HTML grid with a single "save" button.

### 14. `_render_wiki` calls `ensure_wiki()` on every visit (line 2703)

`ensure_wiki` creates directories and seeds pages. It's idempotent but does filesystem I/O on every render. **Change:** guard with a session-state flag (like `wiki_seeded` at line 358).

### 15. `_wiki_context_for_question` uses a hardcoded keyword list (lines 760-802)

~100 keywords across 4 domains. If the user asks about "bike fit" (in the health list) or "FTP" (in the performance list), it works. But "how do I improve my climbing" doesn't match any keyword. **Change:** use a simple embedding similarity or just always inject the wiki index (it's small).

### 16. Module-level `sys.path` manipulation (lines 34-36)

```python
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))
```
This is needed because `streamlit run src/visualize.py` sets cwd to `src/`. But it's a code smell — the package should be installed (or run as `python -m src.visualize`). **Change:** use `python -m src.visualize` or install the package.

### 17. `import json` appears 5+ times inside functions

Lines 583, 902, 2324, 2520, and others. Move to module top.

### 18. `import html` appears twice (lines 2641, 3108)

Move to module top.

### 19. `import math` inside `_render_map` (line 1530)

Move to module top.

### 20. `import threading` inside `_render_dashboard_coach` (line 977)

Move to module top.

## Cross-cutting

- The DB connection in session state (line 337) is shared across all pages, but `_render_garmin_setup` opens its own connections (finding 4). Inconsistent.
- The `st.rerun()` calls after every state change (sync, profile save, journal clear, etc.) are the Streamlit idiom but make the control flow hard to follow.
- The sync progress system (`_wait_for_task`, `_render_sync_progress`, `_sync_progress_callback`) is ~200 lines of complex state management. It works but is fragile — the `sync_origin`/`sync_mode`/`syncing`/`rearsing` flags interact in non-obvious ways.