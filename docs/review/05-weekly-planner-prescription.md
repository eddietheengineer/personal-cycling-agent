# Review: `src/analytics/weekly_planner.py` + `prescription_engine.py`

## weekly_planner.py (925 ln)

7-day plan generation: rules-based (`generate_weekly_plan`) and LLM-based (`generate_ai_plan` with validation retry), shared `PlanningContext`, `validate_plan` constraint checker, JSON persistence.

### Findings

1. **`_project_ctl_atl` (lines 183-204) is dead code.** `project_tsb` (lines 403-426) does the identical computation (same alpha formulas, same loop) and is what everything calls. `_project_ctl_atl` has zero callers. **Change:** delete it.

2. **`_select_session_type` (lines 235-294) is dead code.** `generate_weekly_plan` has its own inline `session_pattern`/`session_map` (lines 579-587) and never calls `_select_session_type`. The two implementations also disagree: `_select_session_type` has readiness/TSB-based early returns and a per-weekday-count pattern table; the inline version just rotates `["endurance", "threshold", "vo2"]`. **Change:** delete `_select_session_type` (or make the rules planner use it — but the inline version is what's tested, so delete).

3. **`session_map` is duplicated** (lines 281-288 in `_select_session_type` and 580-587 in `generate_weekly_plan`) with identical values. Same for the readiness thresholds (40/55 in `_select_session_type` vs 60 in `validate_plan` line 512) — three different readiness cutoffs across the module. **Change:** one `SESSION_MAP` constant, one readiness policy.

4. **`_load_profile` parses markdown by line-prefix (lines 155-173).** It reads `user_profile.md` and extracts `- key: value` lines, lowercasing keys and replacing spaces with underscores. So `FTP (Watts): 250` becomes `ftp_(watts)`. This is fragile:
   - Keys with different spacing/case in the profile file silently change the lookup key.
   - `generate_weekly_plan` looks up `profile.get("ftp_(watts)")` (line 550) — the literal key includes the parentheses. If the profile says `FTP (watts): 250` (lowercase w), the lookup misses and it falls back to `ctx.cp`.
   - `build_planning_context` looks up `max_session_duration`, `tsb_floor`, `primary_goal` (lines 344-346) — these must match the profile's bullet text exactly.
   
   The profile is a free-form markdown file edited in the UI (visualize.py Profile page). **Change:** either make the profile a structured file (YAML/JSON sidecar) or define the expected keys in one place and validate at parse time. At minimum, grep the Profile page's save code (visualize.py) to confirm the bullet format matches these lookups.

5. **`build_planning_context` makes a blocking weather API call (lines 348-354).** `get_weekly_forecast` is called synchronously every time a plan is built. Both the rules and AI paths call `build_planning_context`, so every plan generation hits the weather API. If the API is slow/down, plan generation hangs or degrades. There's no timeout visible here (check `services/weather.py` — it has `HTTP_TIMEOUT_SEC = 10`). **Change:** cache the forecast per day (it doesn't change intra-day) and fail soft to "no forecast" on timeout.

6. **`generate_ai_plan`'s retry loop has a latent `UnboundLocalError` (lines 832-835).** On attempt 0, `_build_prompt()` is called with no feedback. On attempts 1-2, it references `last_errors` — which is only assigned at line 852 *after* the LLM call. If attempt 0's LLM call succeeds but `_parse_llm_response` returns None, the function returns early (line 848) — fine. But if attempt 0's `generate()` raises, it `continue`s to attempt 1, which references `last_errors` before it's ever assigned. **Change:** initialize `last_errors = []` before the loop.

7. **`_parse_llm_response` uses a greedy regex (line 792).** `re.search(r'\[[\s\S]*\]', response)` matches from the *first* `[` to the *last* `]` in the response. If the LLM wraps the JSON in prose that contains brackets (e.g. "Here is your plan [note: ...] [...]"), the match is garbage. Standard fix: find the first `[` and last `]` explicitly, or use a JSON decoder with `raw_decode` starting at the first `[`.

8. **`_raw_to_days` maps by weekday, not date (lines 802-807).** The LLM is told "day 0 = today" and given dates, but the parser looks up `weekday_to_date[weekday]` — a dict keyed by weekday (0-6). If the LLM returns two days with the same weekday (or a wrong weekday), dates collide or fall back to `ctx.day_slots[weekday].date`. The prompt asks for both `date` and `weekday` fields, but only `weekday` is used. **Change:** map by the `date` field (which the LLM is explicitly given) and use `weekday` only as a fallback.

9. **Weekly TSS target formula `ctx.current_ctl * 7 / 30` (lines 680, 780, 863).** This assumes weekly TSS should be ~23% of CTL, which is a heuristic for a specific training frequency. For a 3-day/week rider with 50-90 TSS sessions, weekly TSS is 150-270; CTL 100 gives a target of 23.3 — the scaling at lines 682-688 would shrink every session to ~10% of its planned TSS. **Check:** is this formula intentional? It looks like it was derived for a different unit (maybe daily average TSS × 7?). The AI prompt also tells the LLM "Total weekly TSS target: ~{ctx.current_ctl*7/30:.0f}" (line 780) — the LLM is being asked to hit a target that the rules engine then rescales anyway.

10. **`load_weekly_plan` staleness check (lines 906-910).** `if plan_start < today: return None`. A plan generated yesterday for "today + 6 days" is discarded the next day even though 6 of its 7 days are still in the future. The UI (visualize.py:3040) calls `load_weekly_plan` to display the plan — so the plan disappears from the UI after day 1. **Change:** consider showing the remaining days of a stale plan, or regenerate.

11. **`validate_plan`'s TSB check uses `target_tss` as if it were daily TSS (line 524).** `daily_tss = [d.target_tss if not d.rest_day else 0.0 for d in days]` — but `target_tss` is the *session* TSS, and a day could in principle have two sessions. Currently the planner makes at most one session per day, so it's fine, but the validation would silently under-count if that ever changes.

## prescription_engine.py (385 ln)

3-index readiness scoring (subjective/autonomic/fitness) + pain veto + edge-case overrides + hard guardrails → adjusted TSS and zone.

### Findings

1. **The readiness index silently degrades when inputs are missing (lines 93-135).** Each sub-index only adds terms for non-None inputs, but the *weights aren't renormalized*. If `prs` is None (no morning check-in), the subjective index is missing its 0.35 weight — a rider with perfect sleep/stress/soreness gets subjective = 0.65 max, not 1.0. The composite then treats "no data" as "worse than average" rather than "unknown". The `confidence` field (line 289) is binary ("high" if PRS present, else "low") and doesn't reflect partial data. **Change:** renormalize weights over available inputs, or carry a per-index data-availability flag into the composite.

2. **`main.py` feeds it almost nothing (main.py:691-702).** The only populated fields are `rmssd`, `rmssd_baseline`, `rmssd_std`, `resting_hr`, `rhr_baseline`, `rhr_std`, `ctl`, `atl`, `acwr`, `planned_tss`. That means:
   - Subjective index = 0 (no PRS/soreness/stress/sleep) → composite = 0.30×autonomic + 0.30×fitness.
   - Pain veto, edge cases, sleep debt, jet lag, altitude, illness — all inert (default values).
   - `acwr` comes from `training_load.get("acwr")` — but `training_load_to_dict` returns `fb` (fitness-fatigue ratio), **not `acwr`**. So `acwr` is always None and guardrail H6 never fires. **Verify and fix the key** (either add `acwr` to the training-load dict or compute ATL/CTL here).
   
   The engine is a sophisticated 385-line system that, in production, reduces to "autonomic + fitness index → maybe reduce TSS". Either wire in the morning check-in data (it's in the DB — `store_morning_checkin`/`get_morning_checkin`) or shrink the engine to what's actually used.

3. **`apply_edge_case_overrides` uses an if/elif chain (lines 171-218) so only the *first* matching edge case applies.** A rider who is both travel-fatigued *and* has chronic life stress only gets the travel factor. These should be independent (take the min of all applicable factors), like `apply_hard_guardrails` does.

4. **`_select_zone`'s zone map uses different session-type names than the rest of the codebase (lines 376-384).** Here: `tempo`, `sweet_spot`, `vo2max`, `intervals`. Everywhere else (weekly_planner, session_map): `endurance`, `threshold`, `vo2`, `anaerobic`, `mixed`. `inp.planned_type` defaults to `"endurance"` which *is* in the map, but any other type from the planner (`threshold`, `vo2`) misses the map and falls back to `"Z2"` — a VO2max session gets prescribed Z2. **Change:** unify session-type vocabulary across the codebase (one constant list).

5. **`duration = int(60 * total_factor)` (line 328) is arbitrary.** A full-intensity day gets 60 min regardless of the planned session type (a VO2 session is typically 60-90 min of intervals; a threshold session 45-60). The duration doesn't come from the plan, it's derived from the load factor. This output (`daily_plan`) is only consumed by `main.py:707` and stuffed into `analysis["prescription_engine"]` — which, like `ml_prediction`, may never reach the LLM prompt (verify in prompt_builder review).

6. **`"Part 22"` / `"Part 23"` in docstrings (lines 163, 225).** References to a document structure (presumably `docs/TRAINING_PRESCRIPTION.md`) that doesn't exist in the code. Stale provenance comments.

## Cross-cutting

- **Two readiness systems, three prescription paths.** The codebase now has: (a) `readiness.py` composite score + Kiviniemi states, (b) `prescription_engine.py` 3-index scoring, (c) `weekly_planner.py` rules, (d) `weekly_planner.py` AI plan, (e) `main.py` LLM prescription via `build_system_prompt`. They overlap heavily (all compute "how hard should I train today") with different inputs, weights, and outputs. This is the biggest architectural smell in the analytics layer. **Recommendation:** pick one readiness→prescription pipeline. The weekly planner (rules + AI with validation) is the most complete and is wired to the UI; the prescription engine and the `run_prescribe` LLM path look like earlier iterations that were never removed.

## Follow-ups for later reviews

- [ ] `agent/prompt_builder.py`: confirm whether `prescription_engine` / `ml_prediction` results reach the LLM prompt.
- [ ] `visualize.py` Profile page: confirm the markdown bullet format matches `_load_profile`'s key lookups (`ftp_(watts)`, `max_session_duration`, `tsb_floor`, `primary_goal`).
- [ ] `services/weather.py`: check `get_weekly_forecast` timeout and failure behavior (called synchronously in `build_planning_context`).
- [ ] `tests/test_weekly_planner.py`: check what's actually pinned — the TSS target formula (finding 9) and the session rotation.