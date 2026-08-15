# Review: `src/ui_helpers.py`

294 lines. Pure UI helper functions extracted from `visualize.py`: formatting, downsampling, zone definitions, zone chart builder, profile parsing.

## Findings

### 1. `_get_units_system` re-reads and re-parses the profile file on every call (lines 68-86)

Each call opens `user_profile_path()`, reads the full text, and scans for `- Units:`. This is called from `_format_distance`, `_format_elevation`, `_format_speed_label`, `_format_altitude_label` — potentially dozens of times per page render. **Change:** cache the result in `st.session_state` or read it once per render.

### 2. `_parse_profile_text` only updates keys already in the `profile` dict (line 280)

```python
k = _PROFILE_KEY_MAP.get(key, key)
if k in profile:
    ...
```
If the profile markdown contains a key that's not in the initial `profile` dict (e.g. a new field added to the template), it's silently ignored. The caller must pre-populate the dict with all expected keys. **Change:** use `profile.setdefault(k, val)` or document the contract clearly.

### 3. `_parse_profile_text` int parsing grabs the first number in the value (line 286-289)

`m = re.search(r"(\d+)", v)` — for a value like `"75 kg (165 lbs)"`, it extracts `75` (correct). For `"1/2 hour"`, it extracts `1` (wrong). For `"Zone 2 (56-75%)"`, it extracts `2` (wrong). **Change:** parse the value more carefully (e.g. `float(v.split()[0])` with a fallback).

### 4. Zone ranges have gaps (lines 129-144)

Power zones: Z2 ends at 0.75, Z3 starts at 0.76 — a value of 0.755 falls in neither zone (`_zone_for_value` returns -1). Same for Z3/Z4 (0.90/0.91) and Z4/Z5 (1.05/1.05 — actually contiguous). HR zones: Z1 ends at 0.58, Z2 starts at 0.59 — gap at 0.585. **Change:** make ranges contiguous (Z2: 0.55-0.76, Z3: 0.76-0.91, etc.) or use `lo <= ratio < hi` with the last zone as `lo <= ratio`.

### 5. `_build_zone_chart` takes `st` as a parameter (line 172)

The docstring explains: "passed in to avoid a hard module-level dependency on Streamlit." This is a code smell — the function needs `st.get_option("theme.base")` (line 188) for the theme. **Change:** pass `theme: str` as a parameter instead of the whole `st` module.

### 6. `_downsample` uses `np.arange` but returns Python lists (lines 115-116)

`idx = np.arange(0, n, step)[:max_points]` then `[elapsed[i] for i in idx]` — converts numpy indices back to Python list indexing. For large arrays this is fine, but the numpy import is only needed for `arange`. **Change:** use `range(0, n, step)[:max_points]` (pure Python, no numpy dependency).

### 7. `_stream_id` duplicates the prefix-stripping logic in `main.py` (lines 89-93)

`main.py:224-225` and `main.py:276-277` both do `if sid.startswith("garmin_"): sid = sid[len("garmin_"):]`. This is the third copy (the others are in `main.py`). **Change:** import `_stream_id` from `ui_helpers` in `main.py`, or move it to a shared utility.

### 8. `_HR_RANGES` is imported by `analytics/hr_training_load.py` (per `06-analytics-remaining.md`)

The analytics module imports a UI constant. This is a layering violation: analytics should not depend on UI. **Change:** move `_HR_RANGES` to `src/config/constants.py` or `src/analytics/` and import from there in both places.

## Minor

- `_format_duration` (lines 18-27): handles `None` and negative values, but not `inf`/`nan`.
- `_distance_km` (lines 30-34): only formats metric; the imperial path is in `_format_distance`. The two functions overlap — `_distance_km` is a subset of `_format_distance`. **Change:** delete `_distance_km` and use `_format_distance` everywhere.