# Agent Instructions

## Versioning

Every time you commit and push, increment the patch version by 1 in `personal_cycling_agent/config.json`:

```json
  "version": "X.Y.Z",  // bump Z by 1 on each commit
```

Example: `0.0.1` → `0.0.2` → `0.0.3`

**CRITICAL: `personal_cycling_agent/config.json` is a JSON file, NOT YAML.**
Home Assistant's add-on store requires valid JSON. Never rename it to `.yaml`
or write YAML content into it — HA will reject the add-on entirely.
## Development Workflow

### Option A: VS Code Devcontainer (isolated, official HA approach)
1. Open this repo in VS Code with the Dev Containers extension.
2. VS Code will prompt to reopen in container — accept.
3. Run **Terminal → Run Task → Start Home Assistant**.
4. Open `http://localhost:7123` — your add-on is available as a local add-on.
5. Edit files in VS Code and click **Rebuild** in the local HA UI.

### Option B: Docker container (active deployment)

The app runs in a Docker container named `pca` (image `pca-test`) on port 8501. The image holds only the **dependencies** (Python + `requirements.txt`); the container **bind-mounts the live `src/`** over the baked-in copy, so the running app is byte-for-byte your working tree.

**No rebuild needed when you edit code.** The bind-mount means the container always reflects your working tree (uncommitted changes included) — this is the pre-commit preview. You only rebuild the image when `requirements.txt` changes.

```bash
cd ~/personal-cycling-agent/personal_cycling_agent
docker build --no-cache -t pca-test .          # only when requirements.txt changes
docker stop pca && docker rm pca
docker run -d --name pca -p 8501:8501 \
  -v /home/joshua/cycling-agent-data:/data \
  -v /home/joshua/personal-cycling-agent/personal_cycling_agent/src:/app/src \
  -e CYCLING_AGENT_VAULT=/data \
  pca-test
```

**The `-v .../src:/app/src` bind-mount is the key.** It overrides the baked-in source with your live working tree, so edits appear in the running app immediately (no rebuild). Verify it's live: `docker exec pca stat -c %i /app/src/visualize.py` should match `stat -c %i personal_cycling_agent/src/visualize.py` on the host.

**The `-e CYCLING_AGENT_VAULT=/data` flag is required.** The app resolves the vault path from `CYCLING_AGENT_VAULT` > `DATA_DIR` > `~/cycling-agent-data`. Inside the container the user is `cycling`, so `~` expands to `/home/cycling` — not the mounted `/data`. Without the env var the app creates a fresh empty database, losing all historical data.

**Rebuild the image (with `--no-cache`) only when `requirements.txt` changes.** `--no-cache` is still required for dependency changes (Docker layer caching can serve stale packages). For source-only changes, no rebuild is needed — the bind-mount handles it.

### After Every Commit

The local `pca` container already reflects your working tree (via the bind-mount), so **no local rebuild is needed** after a commit. The commit/push updates the GitHub repo, which is what the HA add-on builds from:

1. `git commit` + `git push`
2. Bump the patch version in `personal_cycling_agent/config.json` (see Versioning).
3. In HA: **Check for updates** → **Rebuild** (the add-on builds from the pushed `main`).

The local `pca` container is a **pre-commit preview** of your working tree; the HA add-on is the **committed** state. After you push and rebuild HA, they converge on the same commit.

**HA Add-on deployment:** Add the GitHub repo as a custom addon store in HA (**Settings → Add-ons → Add-on Store → Repositories →** `https://github.com/eddietheengineer/personal-cycling-agent`). After pushing, click **Check for updates** in the Add-on Store, then **Rebuild** in the add-on UI. No local sync needed.

**Source layout:** `personal_cycling_agent/src/` is the single source of truth. Root `src/` is a symlink to `personal_cycling_agent/src/`. All edits go to `src/` (follows the symlink). Docker builds from `personal_cycling_agent/` where real files live.

## Garmin Data Source

Garmin data comes from the official **Garmin Connect Personal API** — the only
backend. Implemented in `src/ingestion/garmin_personal.py` (client + wellness
sync) with shared FIT parsing, rate limiting, and the batch activity pipeline
in `src/ingestion/garmin_pipeline.py`. The old unofficial
`garminconnect`/`garmin-auth` email+MFA backend (`garmin_connect.py`) has been
deleted; `GARMIN_BACKEND`, `GARMIN_EMAIL`, `GARMIN_PASSWORD`, and
`GARMIN_TOKENSTORE` are no longer read anywhere (stale keys in a user's
`config.env` are simply ignored).

### Personal API notes

- Base URL `https://personal.connectapi.garmin.com`; auth is
  `Authorization: ApiKey <key>` for the token endpoint, then `Bearer <token>`.
  The key is stored in `config.env` as `GARMIN_PERSONAL_API_KEY`; the Bearer
  token is cached in `~/cycling-agent-data/.garmin_personal_token.json`.
- The activity-search endpoint **500s for `limit > 50`** —
  `PERSONAL_MAX_ACTIVITY_LIMIT = 50`. `PersonalActivityClient.get_activities`
  hides this cap by paginating internally, so the batch pipeline can request
  its normal 100-item pages.
- FIT downloads require an `Accept` header including
  `application/x-zip-compressed` (or `*/*`); `application/zip` alone → 406.
- The Personal API does not expose the 4 `metrics-service` charts (endurance
  score, hill score, training readiness, morning training readiness); those
  fields are left empty.
- The activity path runs the shared `_sync_activities_batch` /
  `_fetch_activity_streams` pipeline via a `PersonalActivityClient` adapter
  (`get_activities` / `count_activities` / `download_activity`), so FIT
  parsing and stream storage are shared with the wellness path's helpers.
- Public entry points are `sync_garmin()` (wellness) and `sync_activities()`
  in `garmin_personal.py`; there is no pause circuit breaker (no login to
  fail) — `is_sync_paused` no longer exists.

## Page Specs — Layout & Invariants

Sidebar navigation order: Dashboard, Activities, Trends, Map, Profile, Settings.
Default page: Dashboard. Page routing via `st.session_state.nav_page`.

### Dashboard (`_render_dashboard`)

**Sections (top to bottom):** 7-day week strip, sync progress (dashboard origin only), readiness card, morning check-in, compact coach chat.

**Buttons:**
- `🔄 Sync` (week strip header) — syncs last 1 day, sets `sync_origin="dashboard"`. Progress shows **only** on Dashboard.
- `📊 Rules` (week strip header) — generates weekly plan via rules engine.
- `🤖 AI` (week strip header) — generates weekly plan via LLM.

**Coach chat (compact):** Last 6 messages, text input, Send/Clear buttons. Shares `coach_messages` session state with full Coach page.

**Invariant:** Dashboard sync progress MUST NOT appear on any other page.

### Activities (`_render_activity_detail`)

**Sections:** Activity selectbox, metadata cards (date/duration/distance/calories/power/HR), computed metrics (CP/NP/IF/TSS/VI/W'/decoupling), stream charts (power/HR/speed/cadence/altitude).

**Invariant:** Read-only page. No buttons that trigger background tasks.

### Trends (`_render_trends`)

**Sections:** Date range selectbox (This Year/90d/30d/All Time), CTL/ATL/TSB chart, CP chart, wellness charts (HRV/RHR/weight/sleep/stress — conditional on data).

**Invariant:** Read-only page. No buttons that trigger background tasks.

### Map (`_render_map`)

**Sections:** City text input, radius slider, route stats, scatter map (Mapbox or geo fallback).

**Invariant:** Read-only page. Caches geocode results in `_geocode_cache` session state.

### Profile (`_render_profile`)

**Sections:** Identity (name/weight/height), training discipline, physiological baselines (FTP/max HR/resting HR/gender/LT1/LT2), goals & constraints, equipment, TSB floor, location & schedule. Schedule config grid (7×24).

**Buttons:** `Save Profile` (writes `config.user_profile_path()`), `Save Schedule`.

### Settings (`_render_garmin_setup` + `_render_llm_settings` + `_render_memory_settings`)

**Sections (top to bottom):** Garmin Personal API key form, sync buttons, sync progress (settings origin only), LLM endpoint config, memory journal.

**Buttons:**
- `Sync All Historical Data` — syncs all history (3650 days, unbounded), sets `sync_origin="settings"`. Progress shows **only** on Settings.
- `Reparse FIT` — re-parses all FIT files, sets `sync_origin="settings"`. Progress shows **only** on Settings.
- `💾 Save` (LLM) — saves LLM config.
- `🔍 Test Connection` (LLM) — tests LLM endpoint.

**Invariant:** Settings sync/reparse progress MUST NOT appear on any other page.

### Coach (section within Dashboard, NOT a separate page)

The compact coach chat is a section within Dashboard (`_render_dashboard_coach`), not a separate nav page. There is also a full `_render_coach()` function with `_render_sync_controls()` but it is **dead code** — not called from the sidebar navigation or main dispatch. If `_render_coach()` is ever wired up, it will need its own nav entry.

**Dashboard coach chat:** Last 6 messages, text input, Send/Clear. Shares `coach_messages` with full Coach.

### Sync Progress — Cross-page Rules

`_render_sync_progress(origin)` is called from Dashboard, Settings, and `_render_sync_controls` (dead code, `origin="coach"`). The `origin` parameter gates visibility:

- Dashboard sync (`sync_origin="dashboard"`, `sync_mode="dashboard"`) → progress shows **only** on Dashboard. Uses `BackgroundSync`, sets `syncing=True`.
- Settings sync (`sync_origin="settings"`, `sync_mode="all"`) → progress shows **only** on Settings. Uses `BackgroundSync`, sets `syncing=True`.
- Settings reparse (`sync_origin="settings"`, no sync_mode) → progress shows **only** on Settings. Uses `BackgroundTask`, sets `rearsing=True` (NOT `syncing`).

`sync_mode` values: `"dashboard"` (1-day sync), `"all"` (full historical), `"update"` (Coach 7-day — dead code), `"prescribe"` (Coach prescription — dead code).

`_clear_sync_flags()` clears `syncing`, `rearsing`, `sync_origin`, and `sync_mode` when tasks complete.

**NEVER** move `_render_sync_progress` to run before page routing. It belongs where the sync buttons are.

