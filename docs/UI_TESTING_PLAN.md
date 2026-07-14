# UI Testing & Robustness Plan

## Problem Statement

Changes to the Streamlit dashboard feel fragile — fixes appear to address symptoms rather than root causes. After analysis, the testing strategy has fundamental gaps that create a false sense of security:

1. **~30 tautological tests** in `test_visualize.py` assert hardcoded constants (e.g., `assert True`, `assert 0 >= 0`) and always pass, regardless of actual UI behavior.
2. **Render functions are never tested** — `_render_checkin()`, `_render_activity_detail()`, etc. are never called in any test.
3. **AppTest segfaults** on Python 3.14 + pyarrow, blocking the integration-level test approach.
4. **13 readiness tests are currently failing** — broken tests erode trust in the entire suite.
5. **Silent data bug**: `store_morning_checkin` maps `stress` → `life_stress`, but the table column is `stress`. The field is silently dropped on every save.
6. **Dual source tree**: `src/` and `personal_cycling_agent/src/` are near-copies that diverge silently.
7. **No CI/CD**: tests are never run automatically.

---

## Phase 1: Fix the Broken Foundation

### 1a. Fix the 13 failing `test_readiness.py` tests

The readiness module has drifted from its tests. Investigate each failure:

| Test | Likely cause |
|------|-------------|
| `test_known_values` | Expected values outdated after algorithm change |
| `test_coping_state` | Threshold or scoring logic changed |
| `test_coping_with_varied_baseline` | Baseline computation changed |
| `test_missing_rmssd_only` | Edge case handling changed |
| `test_both_missing_raises` | Error behavior changed |
| `test_single_record` | Minimum data requirement changed |
| `test_confidence_*` (2 tests) | Confidence calculation changed |
| `test_sympathetic_stress_missing_rmssd` | State classification changed |
| `test_parasympathetic_hyperactivity_missing_rmssd` | State classification changed |
| `test_serialization` (2 tests) | `to_dict()` method signature changed |
| `test_skips_unparseable_dates` | Date parsing behavior changed |

**Action**: Read `src/analytics/readiness.py`, compare current behavior against test expectations, and update tests to match the current intended behavior (or fix the code if tests reveal a regression).

### 1b. Remove all tautological tests

Delete or rewrite these tests from `test_visualize.py` that provide zero coverage:

- `test_checkin_slider_range` — asserts `[1,2,3,4,5]` values are in 1-5 (always true)
- `test_checkin_checkbox_types` — asserts `isinstance(True, bool)` (always true)
- `test_sync_button_disabled_logic` — `assert not False` (literal constant)
- `test_reanalyze_always_enabled` — `assert True` (literal constant)
- `test_profile_discipline_options` — asserts hardcoded list has 4 items
- `test_profile_numeric_non_negative` — asserts `0 >= 0`
- `test_map_radius_constraints` — asserts hardcoded `10 > 0`
- `test_date_input_validity` — asserts regex matches `date.today().isoformat()`
- `test_email_format_validation` — tests hardcoded strings, not actual validation
- `test_mfa_code_length` — tests hardcoded strings, not actual validation
- `test_profile_text_fields_not_empty_on_save` — tests hardcoded empty string
- `test_city_input_default` — asserts hardcoded string is non-empty
- `test_page_list_complete` — asserts `len(["Check-in", ...]) == 6`
- `test_page_dispatch_logic` — asserts string starts with `_render_`
- `test_sync_days_mapping` — asserts hardcoded dict values
- `test_auth_state_machine_transitions` — asserts set membership of hardcoded strings
- `test_sync_modes` — asserts `len({"update", "all", "reanalyze"}) == 3`
- `test_credentials_check` — asserts `has_credentials is False` (env is always empty in test)

---

## Phase 2: Extract Testable Logic from `visualize.py`

The 1421-line monolith mixes Streamlit widget calls with pure computation. Extract the computation into `src/ui_helpers.py` so it can be tested without a running Streamlit instance.

### Pure functions to extract

| Function | Current location | What it does |
|----------|-----------------|--------------|
| `_format_duration()` | visualize.py:104 | ms → "1h 2m 3s" |
| `_distance_km()` | visualize.py:116 | cm → "50.00 km" |
| `_stream_id()` | visualize.py:123 | Strip `garmin_` prefix |
| `_downsample()` | visualize.py:90 | Reduce data points for charting |
| `_zone_for_value()` | visualize.py:145 | Map value to zone index |
| `_make_zones()` | visualize.py:164 | Build zone tuples |
| `_elapsed_to_minutes()` | visualize.py:100 | Seconds → minutes |
| Zone constants | visualize.py:132-179 | `_ZONE_RANGES`, `_HR_RANGES`, colors |
| Profile parser | visualize.py (inline in `_render_profile`) | Markdown → dict |
| Check-in assembler | visualize.py (inline in `_render_checkin`) | Form data → DB dict |
| Trend data queries | visualize.py (inline in `_render_trends`) | DB → chart DataFrames |
| Activity list formatter | visualize.py (inline in `_render_activity_detail`) | DB rows → display dicts |

### Extraction rules

- Extracted functions take explicit parameters (no `st`, no `db` module globals)
- Functions that need DB access take a `CyclingDB` instance as parameter
- Functions that need Streamlit (theme detection) take the needed value as parameter
- Keep widget creation (`st.slider`, `st.form`, etc.) in `visualize.py` — only extract the data/logic layer

---

## Phase 3: Write Real Tests

### 3a. Unit tests for extracted helpers (`tests/test_ui_helpers.py`)

New file with tests that verify actual input → output behavior:

```
TestFormatDuration:
  - test_hours_minutes_seconds: _format_duration(3661000) == "1h 1m 1s"
  - test_minutes_seconds: _format_duration(90000) == "1m 30s"
  - test_none: _format_duration(None) == "—"
  - test_zero: _format_duration(0) == "0m 0s"

TestDistanceKm:
  - test_normal: _distance_km(5000000) == "50.00 km"
  - test_zero_returns_dash: _distance_km(0) == "—"
  - test_none_returns_dash: _distance_km(None) == "—"
  - test_precision: _distance_km(1234567) == "12.35 km"

TestStreamId:
  - test_strips_prefix: _stream_id("garmin_12345") == "12345"
  - test_no_prefix: _stream_id("12345") == "12345"

TestDownsample:
  - test_no_op_under_limit: returns input unchanged when len <= max_points
  - test_reduces_over_limit: returns exactly max_points items
  - test_preserves_order: output indices are monotonically increasing

TestZoneForValue:
  - test_zone_1: value at 50% of FTP → zone 0
  - test_zone_3: value at 85% of FTP → zone 2
  - test_zone_5: value at 110% of FTP → zone 4
  - test_zero_threshold: returns -1 (division by zero guard)
  - test_boundary_values: exact boundary values map to correct zone

TestProfileParsing:
  - test_full_profile: parse complete markdown → correct dict
  - test_missing_fields: partial profile → defaults for missing fields
  - test_non_numeric_values: graceful handling of non-numeric where int expected
  - test_empty_file: empty string → all defaults

TestCheckinDataAssembly:
  - test_form_to_db_mapping: form dict → correct DB column names
  - test_stress_field_mapping: verify stress → life_stress mapping (or fix the bug)
  - test_existing_checkin_defaults: load existing → correct default values
  - test_none_values_become_defaults: None from DB → slider default of 3
```

### 3b. Data contract tests (replace tautological ones in `test_visualize.py`)

Keep only the tests that actually exercise `CyclingDB` or the config module. Rewrite removed tests as real tests from 3a.

### 3c. AppTest integration tests (`test_visualize_streamlit.py`)

Keep existing AppTest tests (they are real integration tests). Add:

- `test_checkin_save_persists_to_db`: fill form → submit → verify DB row
- `test_activity_detail_shows_metrics`: with seeded data, verify metric display
- `test_trends_shows_charts`: with seeded wellness data, verify chart elements exist
- `test_profile_save_updates_env`: edit profile → save → verify persistence

Note: These may need to be gated behind `@pytest.mark.skipif` if AppTest segfaults on Python 3.14.

---

## Phase 4: Fix the `stress` → `life_stress` Data Bug

**The bug**: `CyclingDB.store_morning_checkin()` maps the key `stress` to `life_stress` when building the INSERT statement, but the `morning_checkin` table schema has a column named `stress`, not `life_stress`. The stress value from the UI is silently dropped on every save.

**Fix options**:
- **Option A**: Rename the table column from `stress` to `life_stress` (requires migration)
- **Option B**: Fix the mapping in `store_morning_checkin()` to use `stress` (simpler, no migration)
- **Option C**: Both — rename for clarity, add migration for existing data

**Action**: Implement Option B (simplest fix), add regression test in `test_ui_helpers.py`.

---

## Phase 5: CI Pipeline

Create `.github/workflows/test.yml`:

```yaml
name: Tests
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - run: pip install pytest
      - run: pytest tests/ -v --tb=short
```

Note: Use Python 3.12 in CI (not 3.14) to avoid the AppTest segfault issue. The add-on Dockerfile already pins a specific version.

---

## Phase 6: Unify Dual Source Tree

Currently `src/` and `personal_cycling_agent/src/` are maintained as near-copies. Per AGENTS.md, Garmin auth changes must be made in both places — this is a maintenance burden and divergence risk.

**Recommended approach**: Single source of truth in root `src/`.

Update `personal_cycling_agent/Dockerfile` to copy from root:

```dockerfile
# Instead of COPY src/ ./src/
COPY src/ ./src/
```

The Docker build context is already the repo root (since `repository.json` and `config.json` are at `personal_cycling_agent/` level, but the build context can be adjusted). If the HA add-on build context is limited to `personal_cycling_agent/`, use a symlink or a pre-build copy step.

**Alternative**: Add a pre-commit hook that fails if the two trees diverge:

```bash
diff -rq src/ personal_cycling_agent/src/ --exclude='__pycache__'
```

---

## Execution Order

| Step | Work | Depends on | Estimated files changed |
|------|------|-----------|----------------------|
| 1 | Fix readiness tests | None | `tests/test_readiness.py`, possibly `src/analytics/readiness.py` |
| 2 | Remove tautological tests | None | `tests/test_visualize.py` |
| 3 | Extract helpers to `src/ui_helpers.py` | None | `src/visualize.py`, new `src/ui_helpers.py` |
| 4 | Write real unit tests for helpers | Step 3 | New `tests/test_ui_helpers.py` |
| 5 | Fix stress/life_stress bug | None | `src/db/store.py`, `tests/test_ui_helpers.py` |
| 6 | Add CI pipeline | Steps 1-2 (tests must pass) | New `.github/workflows/test.yml` |
| 7 | Unify source tree | All above stable | `personal_cycling_agent/Dockerfile`, delete `personal_cycling_agent/src/` |

Steps 1-2 and 5 can be done in parallel. Steps 3-4 are sequential. Step 6 requires green tests. Step 7 is last because it's the most disruptive.

---

## Success Criteria

- [ ] All tests pass (`pytest tests/ -v` exits 0)
- [ ] Zero tautological tests remain (no `assert True`, no hardcoded-only assertions)
- [ ] Every extracted helper function has ≥3 test cases (happy path, edge case, boundary)
- [ ] CI runs on every push/PR and blocks on failure
- [ ] Single source of truth for `src/` (no dual-tree drift)
- [ ] `stress` field persists correctly through check-in save
