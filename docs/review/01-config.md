# Review: `src/config/` package

Files: `__init__.py` (182 ln), `constants.py` (176 ln), `schedule.py` (124 ln), `llm_config.py` (95 ln)

## What it does

- `__init__.py` — vault path resolution (`CYCLING_AGENT_VAULT` > `DATA_DIR` > `~/cycling-agent-data`), loads `config.env` via dotenv, resolves PBKDF2-hashed passwords back to plaintext in a module-level dict, path helpers (`vault_path`, `db_path`, `raw_dir`, `user_profile_path`), and re-exports schedule functions.
- `constants.py` — central numeric constants (half-lives, thresholds, rate limits, unit conversions) with "Used in:" comments.
- `schedule.py` — training schedule JSON persistence + weather location persistence.
- `llm_config.py` — LLM endpoint JSON persistence with env-var fallback.

## Findings

### 1. `DEFAULT_SCHEDULE` is dead code in a legacy format (schedule.py:24-32)

The default uses `{"available": False, "ride_windows": [...]}` — a format that no consumer reads. The only consumers of `load_schedule()` output read `available_hours` (`get_available_days`, `get_available_hours`, visualize.py:3232, weekly_planner.py). `DEFAULT_SCHEDULE` is re-exported from `config/__init__.py:177` but has **zero consumers outside the config package** (verified by grep).

Worse: the fallback path is inconsistent. When the file is missing or corrupt, `load_schedule()` returns `dict(DEFAULT_SCHEDULE)` — entries with `ride_windows` and **no `available_hours` key**. So `get_available_days()` returns `[]` (fine, empty), but any code doing `schedule[day]["available_hours"]` directly would `KeyError`. The UI grid (visualize.py:3232) uses `.get(..., {})` so it survives, but the contract is fragile.

**Change:** make `DEFAULT_SCHEDULE` use the current format: `{"monday": {"available_hours": []}, ...}`. Then the fallback is consistent with the migrated format and the `ride_windows`/`time_slots` branches of `_migrate_entry` only ever run on real legacy files.

### 2. `_migrate_entry` duplicates `_SLOT_TO_WINDOW` (schedule.py:35-40 vs 57-58)

The slot→hours mapping exists twice: as `_SLOT_TO_WINDOW` (dict of start/end) and as an inline `slot_map` inside `_migrate_entry` (dict of ranges). The two can drift. `_SLOT_TO_WINDOW` is only used by `_migrate_entry`'s sibling logic — actually it's not used at all by `_migrate_entry` (which uses its own `slot_map`). **`_SLOT_TO_WINDOW` is dead code.**

**Change:** delete `_SLOT_TO_WINDOW`; keep one mapping.

### 3. Weather location code is misplaced in `schedule.py` (schedule.py:99-124)

`_weather_file`, `load_weather_location`, `save_weather_location` have nothing to do with the training schedule. They're also duplicated in `services/weather.py:83-85` which reads `weather_location.json` directly via `vault_path()` (need to verify whether weather.py uses the schedule.py functions or its own copy — see follow-up).

**Change:** move weather-location persistence to `services/weather.py` (or a small `src/config/location.py`), and have all callers use one implementation.

### 4. Missing blank line between functions (schedule.py:98-99)

`get_available_hours` ends at line 98 and `_weather_file` starts at line 99 with no blank line. Cosmetic, but it's the only PEP8 violation in the package.

### 5. PBKDF2 "hashing" is a verification step, not a hash (config/__init__.py:69-121)

The design stores **both** the PBKDF2 hash and the plaintext (`GARMIN_PASSWORD_RAW`) in `config.env`, then at startup verifies the hash against the raw value and keeps the plaintext in a module dict. The hash therefore provides no confidentiality — the plaintext is on disk in the same file. The only real benefit is integrity (detecting tampering) and keeping the plaintext out of `os.environ`/`/proc/<pid>/environ`.

This is a defensible design for a single-user local app, but the docstring at line 14 ("Passwords are stored as PBKDF2 hashes") overstates it. Also:

- `hash_password()` (line 135) returns `(hash, plaintext)` — the caller is expected to write both to `config.env`. Verify the UI does this (visualize.py Settings page — check during that review).
- The legacy `hash:` SHA-256 branch (lines 106-117) exists for backward compat. If no vaults in the wild use it, delete it.
- `os.environ[var] = ""` (line 103) leaves an **empty** `GARMIN_PASSWORD` in the environment. Any code path that reads `os.environ["GARMIN_PASSWORD"]` directly instead of `get_resolved_credential()` silently gets `""`. Grep shows only `garmin_connect.py:390` uses `get_resolved_credential` — but this is a footgun for future code. Consider `os.environ.pop(var)` instead, so direct readers get the same "missing" behavior as if the var was never set.

### 6. `setup()` is not idempotent-safe for vault moves (config/__init__.py:47-66)

`load_dotenv(..., override=True)` re-reads `config.env` on every `setup()` call. If `setup()` is called twice with different `CYCLING_AGENT_VAULT` values (e.g., a test after the app), the second vault's env vars override the first, but `_resolved_credentials` still holds the first vault's passwords. `get_resolved_credential` checks the dict first, so you'd get vault A's password with vault B's email. Low risk in practice (single process, single vault) but worth a guard: either make `setup()` a no-op after first call, or clear `_resolved_credentials` at the top.

### 7. `constants.py` "Used in:" comments will rot

Every constant block has a `# Used in:` comment. These are already slightly wrong: `ROLLING_CP_WINDOW_DAYS` says "Used in: main.py" — verify; `DEFAULT_ANALYSIS_WINDOW_DAYS` says "readiness.py, store.py, journal.py, scheduler.py". These comments are a maintenance tax and a source of stale docs. The constants are already grouped and named well; the comments add little.

**Change:** drop the "Used in:" comments, or generate them. Keep the group headers.

### 8. `llm_config.py` reads the JSON file on every getter call

`get_llm_base_url()`, `get_llm_api_key()`, `get_llm_model()`, `get_llm_timeout()` each call `load_llm_config()` which opens and parses the file. `llm_client.py` calls these per-request (base URL, key, timeout, model — 4 file reads per LLM call). Not a perf problem at this scale, but the pattern invites it. Also the fallback logic is inverted vs the docstring: the docstring says "vault config > env var > default" but the code does `cfg.get("base_url", "") or os.getenv(...)` — an **empty string** in the vault file falls through to env, which is probably intended, but a vault value of `""` is indistinguishable from "not set". Fine in practice.

**Change (optional):** cache the loaded config with an mtime check, or just document that the file is read per call.

## Follow-ups for later reviews

- [ ] `visualize.py` Settings page: verify `hash_password()` output is written as both `GARMIN_PASSWORD` and `GARMIN_PASSWORD_RAW`.
- [ ] `services/weather.py`: does it use `schedule.load_weather_location()` or its own file read? (saw `vault_path()` direct access at line 83-85)
- [ ] `main.py`: verify `ROLLING_CP_WINDOW_DAYS` usage matches the constants.py comment.
- [ ] Confirm no vaults in the wild use the legacy `hash:` SHA-256 format before deleting that branch.