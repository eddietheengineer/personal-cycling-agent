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
docker build -t pca-test .
docker stop pca && docker rm pca
docker run -d --name pca -p 8501:8501 \
  -v /home/joshua/cycling-agent-data:/data \
  -e CYCLING_AGENT_VAULT=/data \
  pca-test
```

**The `-e CYCLING_AGENT_VAULT=/data` flag is required.** The app resolves the vault path from `CYCLING_AGENT_VAULT` > `DATA_DIR` > `~/cycling-agent-data`. Inside the container the user is `cycling`, so `~` expands to `/home/cycling` — not the mounted `/data`. Without the env var the app creates a fresh empty database, losing all historical data.

**Do not skip this step.** A `docker restart pca` is insufficient — it restarts the old image. The full rebuild cycle is required.

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
