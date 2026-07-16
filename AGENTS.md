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
2. `docker image rm pca-test` (remove old image to prevent cache reuse)
3. `docker build --no-cache -t pca-test .` (clean rebuild)
4. `docker stop pca && docker rm pca` (remove running container)
5. `docker run -d --name pca -p 8501:8501 -v /home/joshua/cycling-agent-data:/data -e CYCLING_AGENT_VAULT=/data pca-test`
6. Verify the container has the new code: `docker exec pca head -5 /app/src/<changed_file>`

**Never skip steps 2 or 6.** Step 2 prevents Docker from reusing cached layers. Step 6 confirms the running container actually has the new code before declaring the commit done.

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

## Sync Progress UI — Critical Invariants

The sync progress dialog (`_render_sync_progress` in `src/visualize.py`) has requirements that MUST be preserved:

1. **Re-detection is mandatory, but page-scoped.** The function MUST check `bg.is_running` on both `get_default_sync()` and `get_default_task()` before returning early. Session flags (`syncing`/`rearsing`) can be stale or cleared after page navigation — the background task snapshot is the source of truth. Without this check, navigating away and back loses the progress window. **However, the progress window must ONLY appear on the page where the sync was initiated.** (Settings).** `_render_sync_progress()` must only be called from the Settings page — never from Dashboard, Trends, or any other page. A background task running in the background should NOT cause the progress dialog to appear on unrelated pages.

2. **No auto-rerun polling.** NEVER add `time.sleep()` + `st.rerun()` to poll for progress updates. This refreshes the entire page and breaks the UX. The blocking `_wait_for_task` approach is correct — it updates in-place via `st.empty()` placeholders.

3. **Progress must resume from current snapshot.** On re-entry (page refresh or navigation back), `_wait_for_task` MUST read `bg.snapshot()` before rendering so the progress bar, status label, and log show actual state — not "Waiting..."/0%.

4. **Keep `_render_sync_progress` inside Settings page only.** Do NOT move it to run before page routing. It belongs where the sync buttons are. The re-detection (point 1) handles the "navigate away and back" case.

5. **Sync both copies.** Changes to `src/visualize.py` MUST be copied to `personal_cycling_agent/src/visualize.py` before Docker rebuild.
