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

For local development without pushing to GitHub after every change:

### Option A: Local `/addons` directory (quickest)
1. Copy the `personal_cycling_agent/` folder to `/addons/personal_cycling-agent/` on your HA host (via Samba or SSH).
2. In HA Add-on Store → ⋮ → **Check for updates** → install from **Local add-ons** section.
3. Edit files locally, then click **Rebuild** in the add-on UI. No git push needed.

### Option B: VS Code Devcontainer (isolated, official HA approach)
1. Open this repo in VS Code with the Dev Containers extension.
2. VS Code will prompt to reopen in container — accept.
3. Run **Terminal → Run Task → Start Home Assistant**.
4. Open `http://localhost:7123` — your add-on is available as a local add-on.
5. Edit files in VS Code and click **Rebuild** in the local HA UI.
### Option C: Docker container (active deployment)

The app runs in a Docker container named `pca` (image `pca-test`) on port 8501. The source code is baked into the image — it does **not** volume-mount the source directory.

**After any code change to `src/`, you MUST rebuild and restart the container:**

```bash
cd ~/personal-cycling-agent/personal_cycling_agent
docker build --no-cache -t pca-test .
docker stop pca && docker rm pca
docker run -d --name pca -p 8501:8501 \
  -v /home/joshua/cycling-agent-data:/data \
  -e CYCLING_AGENT_VAULT=/data \
  pca-test
```

**CRITICAL: Always use `--no-cache`.** Docker layer caching has caused stale code to ship to the container even after source changes — the build reports success but the running container executes old code. `--no-cache` ensures every rebuild actually copies the current source.

**The `-e CYCLING_AGENT_VAULT=/data` flag is required.** The app resolves the vault path from `CYCLING_AGENT_VAULT` > `DATA_DIR` > `~/cycling-agent-data`. Inside the container the user is `cycling`, so `~` expands to `/home/cycling` — not the mounted `/data`. Without the env var the app creates a fresh empty database, losing all historical data.

**Do not skip this step.** A `docker restart pca` is insufficient — it restarts the old image. The full rebuild cycle is required.

### After Every Commit

**MUST do a force clean rebuild after every commit and push.** Docker layer caching silently serves stale code — `docker build` without `--no-cache` will report success but ship old files. The sequence is:

1. `git commit` + `git push`
2. `python3 ~/sync_to_ha.py` (sync source and data to HA addon folder via SMB)
3. `docker image rm pca-test` (remove old image to prevent cache reuse)
4. `docker build --no-cache -t pca-test .` (clean rebuild)
5. `docker stop pca && docker rm pca` (remove running container)
6. `docker run -d --name pca -p 8501:8501 -v /home/joshua/cycling-agent-data:/data -e CYCLING_AGENT_VAULT=/data pca-test`
7. Verify the container has the new code: `docker exec pca head -5 /app/src/<changed_file>`

**Never skip steps 2, 3, or 7.** Step 2 syncs the latest code to the HA addon share. Step 3 prevents Docker from reusing cached layers. Step 7 confirms the running container actually has the new code before declaring the commit done.

**Note:** `sync_to_ha.py` lives at `~/sync_to_ha.py` (outside the repo). It is gitignored and MUST never be committed.

## Garmin Authentication (garmin-auth 0.3.0)

This project uses `garmin-auth` 0.3.0 for Garmin Connect authentication. The API has specific patterns for MFA handling that must be followed to avoid breaking the auth flow.

### Correct API Usage

**For UI contexts (Streamlit Settings page):**

```python
from garmin_auth import GarminAuth

# Phase 1: Initial login attempt
auth = GarminAuth(
    email=email,
    password=password,
    token_dir=tokenstore,
    return_on_mfa=True,  # CRITICAL: don't prompt interactively
)
result = auth.login()

if result == "needs_mfa":
    # MFA required — save auth instance to session state
    st.session_state.garmin_auth_instance = auth
    # Show OTP input form to user
else:
    # result is a Garmin client — success
    client = result

# Phase 2: Complete MFA (after user enters OTP)
auth = st.session_state.garmin_auth_instance
auth.resume_login(mfa_code)  # Returns Garmin client on success
```

**For non-interactive contexts (background sync, cron jobs):**

```python
auth = GarminAuth(
    email=email,
    password=password,
    token_dir=tokenstore,
    return_on_mfa=True,  # CRITICAL: don't prompt interactively
)
result = auth.login()

if result == "needs_mfa":
    raise RuntimeError(
        "Garmin login requires MFA but running non-interactively. "
        "Please log in via the Settings page first to cache tokens."
    )

client = result
```

### Critical Points

1. **`auth.login()` handles cached tokens automatically.** It tries cached tokens first via `_try_cached_login()` internally, then falls back to fresh login. You do NOT need to call a separate method to check cached tokens.

2. **Always use `return_on_mfa=True` in non-interactive contexts.** This prevents garmin-auth from calling the `prompt_mfa` callback, which would fail in Streamlit/background jobs.

3. **Never call `auth.get_garmin()` — this method doesn't exist in garmin-auth 0.3.0.** The correct method is `auth.login()`.

4. **For MFA completion, call `auth.resume_login(code)` on the SAME instance** that returned `"needs_mfa"`. Creating a new `GarminAuth` instance loses the pending MFA state and will trigger a new login attempt (causing duplicate OTP emails).

5. **Tokens are automatically persisted** by garmin-auth after successful login. They're stored in the `token_dir` directory and reused on subsequent calls.

### Common Mistakes

- Using `prompt_mfa` callback instead of `return_on_mfa=True` — causes interactive prompts in non-interactive contexts
- Calling `auth.get_garmin()` — this method doesn't exist, will raise `AttributeError`
- Creating a new `GarminAuth` instance for MFA completion — loses pending MFA state, triggers duplicate login attempts
- Not checking for `result == "needs_mfa"` — will treat the sentinel string as a valid client
- Calling `auth.login()` twice without saving the instance — triggers multiple login attempts to Garmin servers, may cause rate limiting

### Tokenstore Path Consistency

**CRITICAL:** The `token_dir` parameter must be consistent across all authentication calls.

- The UI login (Settings page) uses `os.environ.get("GARMIN_TOKENSTORE", "")` to get the token directory
- The sync functions (`sync_garmin()`, `sync_activities()`) call `_create_client()` which must use the **same** token directory
- If the paths differ, tokens saved during UI login won't be found during sync, causing fresh login attempts and MFA emails

**Solution:** `_create_client()` should default to `os.getenv("GARMIN_TOKENSTORE", "")` when `tokenstore` parameter is `None`:

```python
def _create_client(tokenstore: str | None = None) -> "garminconnect.Garmin":
    ...
    if tokenstore is None:
        tokenstore = os.getenv("GARMIN_TOKENSTORE", "")
    
    auth = GarminAuth(
        email=email,
        password=password,
        token_dir=tokenstore if tokenstore else "~/.garminconnect",
        return_on_mfa=True,
    )
    ...
```

This ensures both UI login and background sync use the same token directory.

### Files to Update

When modifying Garmin authentication code, update BOTH versions:
- `personal_cycling_agent/src/ingestion/garmin_connect.py` (Home Assistant add-on)
- `src/ingestion/garmin_connect.py` (standalone/development)

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

**Sections (top to bottom):** Garmin login form, sync buttons, sync progress (settings origin only), LLM endpoint config, memory journal.

**Buttons:**
- `Sync All Historical Data` — syncs all history (3650 days, unbounded), sets `sync_origin="settings"`. Progress shows **only** on Settings.
- `Reparse FIT` — re-parses all FIT files, sets `sync_origin="settings"`. Progress shows **only** on Settings.
- `💾 Save` (LLM) — saves LLM config.
- `🔍 Test Connection` (LLM) — tests LLM endpoint.

**Invariant:** Garmin MFA flow lives here. Settings sync/reparse progress MUST NOT appear on any other page.

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

### File Sync Rule

Changes to `src/visualize.py` MUST be copied to `personal_cycling_agent/src/visualize.py` before Docker rebuild. The Dockerfile copies from `personal_cycling_agent/`.
