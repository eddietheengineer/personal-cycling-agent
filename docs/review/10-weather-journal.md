# Review: `src/services/weather.py` + `src/memory/journal.py`

weather.py: 305 lines. journal.py: 119 lines.

## `weather.py`

### 1. `get_location` docstring is wrong (line 77)

> "Get user location from config or Garmin GPS data."

The function checks `WEATHER_LAT`/`WEATHER_LON` env vars and a `weather_location.json` file. It does **not** read Garmin GPS data (the module docstring at line 5 makes the same false claim). **Change:** fix both docstrings, or implement the GPS fallback (latest activity's `activity_routes` first point).

### 2. `get_weekly_forecast` is called on every Dashboard render (visualize.py:531-536)

The Dashboard fetches a fresh 7-day forecast from Open-Meteo on **every Streamlit rerun** (every button click, every input change). No caching. Each call is a blocking `urlopen` with `HTTP_TIMEOUT_SEC` timeout. **Change:** cache the forecast in `st.session_state` with a TTL (e.g. 1 hour) or use `st.cache_data(ttl=3600)`.

### 3. `_hour_idx` is O(n) per lookup, called O(7×3×hours) times (lines 147-154, 171-179)

For each of 7 days × 3 slots × ~3 hours, it linearly scans the 168-entry `h_times` list. That's ~500 string comparisons per forecast. Trivial in absolute terms, but a `dict` lookup would be O(1). **Change:** build `{t[:13]: i for i, t in enumerate(h_times)}` once.

### 4. "Worst code" selection is a boolean sort (line 186)

```python
worst_code = max(slot_codes, key=lambda c: code_map.get(c, "unknown") in ("storm", "rain", "snow"))
```
This picks a code whose condition is in `("storm", "rain", "snow")` — but `max` with a boolean key returns the *first* such code (all True values are equal). It doesn't actually rank storm > rain > snow. A slot with drizzle (code 51) and rain (code 61) picks whichever appears first. **Change:** define an explicit severity ordering (e.g. `{clear: 0, partly_cloudy: 1, cloudy: 2, fog: 2, drizzle: 3, rain: 4, freezing_rain: 5, snow: 5, storm: 6}`) and `max` by severity.

### 5. `find_ride_slot` truncates ride duration (line 227)

`ride_slots_needed = int(ride_duration_hours)` — a 1.5h ride becomes 1 slot (1 hour). A 2.5h ride becomes 2 slots. The fractional hour is silently dropped. **Change:** use `math.ceil(ride_duration_hours)`.

### 6. `find_ride_slot` requires *all* ride hours to be clear (line 275)

`if not ride_clear: continue` — a single hour of drizzle disqualifies the entire slot. Combined with the boolean "worst code" (finding 4), this is overly strict. **Change:** allow a small number of non-clear hours (e.g. score-based: penalize but don't disqualify).

### 7. `import json` at line 14, after other imports

Style: move to the top import block (lines 8-16).

## `journal.py`

### 8. `extract_memories` makes a blocking LLM call in the UI thread (visualize.py:971-974)

After each coach chat exchange, `extract_memories` calls `llm_client.generate()` (blocking HTTP request) to extract memory bullets, then appends them. This happens in the Streamlit main thread (wrapped in a `threading.Thread` at visualize.py:977 — need to verify). If it's synchronous, the UI freezes for the duration of the LLM call. **Change:** confirm it's in a background thread; if not, move it.

### 9. Journal grows unbounded

`append_entry` and `append_conversation` append to `memory_journal.md` forever. `load_recent(30)` only reads the last 30 lines, but the file itself grows without limit. After a year of daily coach chats, the file could be megabytes. **Change:** add a rotation/pruning policy (e.g. keep last 90 days, archive the rest) or a max-file-size check.

### 10. `load_recent` reads the entire file to get the last N lines (lines 32-38)

`load_journal()` reads the whole file, splits into lines, takes the last N. For a large journal this is wasteful. **Change:** read the file in reverse (or use `deque(text.splitlines(), maxlen=n)`).

### 11. `_journal_path` imports `vault_path` inside the function (line 18)

Avoids a circular import, presumably. But `src.config` doesn't import from `src.memory`, so the top-level import should be safe. **Change:** move to module top (verify no circularity).

## Cross-cutting

- Both modules use the lazy `from src.config import ...` pattern inside functions. Consistent with the rest of the codebase but worth a pass to hoist where safe.