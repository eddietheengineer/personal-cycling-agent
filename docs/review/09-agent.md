# Review: `src/agent/` package

Three modules: `llm_client.py` (275 ln), `prompt_builder.py` (291 ln), `db_query.py` (134 ln). The LLM plumbing for the coach chat.

## Findings

### `db_query.py`

#### 1. `ALLOWED_TABLES` is defined but never enforced (lines 17-29)

The set of 11 table names is declared, but `query_db` never checks that the query only touches those tables. The LLM can `SELECT * FROM sqlite_master` or read any table in the database. The keyword/function/pattern blocks are the only real guardrails. **Change:** either enforce table allowlisting (parse the query with `sqlparse` or a regex on `FROM`/`JOIN` clauses) or delete `ALLOWED_TABLES` to avoid implying a guarantee that doesn't exist.

#### 2. Substring keyword blocking is trivially bypassable (lines 69-71)

`if kw in sql_upper` — the keyword `DELETE` is blocked, but so is any query containing the word "delete" in a string literal or column name (e.g. `SELECT * FROM activities WHERE name LIKE '%delete%'`). Conversely, `PRAGMA` is blocked but `sqlite_master` is not — `SELECT * FROM sqlite_master` works and leaks the full schema. **Change:** use a proper SQL parser (e.g. `sqlparse` or SQLite's own tokenizer) to validate the statement is a single SELECT with no subqueries touching system tables.

#### 3. `BLOCKED_FUNCTIONS` includes `HEX`, `CHAR`, `QUOTE`, `PRINTF` (lines 39-43)

These are harmless formatting functions. Blocking them reduces the coach's ability to format output (e.g. `printf('%.1f', value)`). The genuinely dangerous ones (`LOAD_EXTENSION`, `READFILE`, `WRITEFILE`) are already blocked by `BLOCKED_KEYWORDS`. **Change:** remove the formatting functions from the blocklist.

#### 4. DB path is hardcoded (line 98)

`db_path = vault_path / "data" / "cycling_agent.sqlite"` — bypasses `config.db_path()`. If the vault layout changes or the DB is named differently, this breaks silently. **Change:** use `config.db_path("cycling_agent.sqlite")`.

#### 5. Opens a new SQLite connection per query (line 103)

`sqlite3.connect` per call. For the coach chat (1-2 queries per conversation turn) this is fine, but it means the query sees a potentially stale snapshot if a sync is writing concurrently. **Change:** accept an optional `CyclingDB` instance or at least use `config.db_path()`.

### `prompt_builder.py`

#### 6. `WEIGHT_KG` env var is the only weight source (line 65)

`rider_weight = os.getenv("WEIGHT_KG", "unknown")` — but the user profile (loaded at line 63) contains weight, and the `wellness` table has daily weight. The env var is set in `config.env` and may be stale (the user's weight changes; the Profile page updates the markdown, not the env). **Change:** read weight from the profile or the latest wellness record; fall back to the env var.

#### 7. `build_json_context` is dead code (lines 272-291)

No callers in the codebase (grep confirms only the definition). **Change:** delete it.

#### 8. The `analysis` dict is a grab-bag with no schema (lines 154-250)

`build_system_prompt` accepts `analysis: dict[str, Any] | None` and reaches into ~10 keys (`training_load_history`, `training_load`, `power_metrics`, `strain_scores`, `w_prime`, `durability`, `decoupling`, `pmax_estimates`, `three_dim_ir`, `cp`, `feedback`). The caller (`main.py`) builds this dict. If a key is missing, the section is silently skipped. This works but is fragile — a typo in a key name means the LLM never sees that data. **Change:** define a dataclass or TypedDict for the analysis context so missing keys are caught at construction time.

#### 9. Readiness section only shows RMSSD and RHR (lines 84-95)

The readiness dict from `readiness_to_dict()` contains more (state, recommendation, bands), but the prompt only surfaces RMSSD, RHR, and the recommendation string. The TSB/CTL/ATL from the training load section is separate. The LLM doesn't see the full readiness picture (e.g. the individual model's prediction, the 3D IR score is buried in the `analysis` section). **Change:** surface the key readiness numbers (TSB, ACWR, predicted recovery) in the readiness section, not just RMSSD/RHR.

### `llm_client.py`

#### 10. `_split_prompt_into_messages` is a fragile line-based parser (lines 124-182)

It splits the prompt on `\nConversation:` and then parses `USER: ` / `ASSISTANT:` line prefixes. If the LLM's previous response contains a line starting with `USER: ` (e.g. quoting the user), the parser mis-splits. The `ASSISTANT:` check (line 157) doesn't require a space after the colon, but `USER: ` does (line 152) — inconsistent. **Change:** use a delimiter that can't appear in message content (e.g. `\n---USER---\n`) or pass structured messages instead of a combined string.

#### 11. `max_tokens` is hardcoded to 2048 (line 112)

A weekly prescription or a detailed analysis can easily exceed 2048 tokens. **Change:** make it configurable via `llm_config` or scale it with the prompt length.

#### 12. `temperature` is hardcoded to 0.3 (line 111)

Fine for prescriptions, but the coach chat (conversational) might benefit from a slightly higher temperature. **Change:** make it a parameter of `generate()`.

#### 13. `_ensure_init` / `_initialized` pattern (lines 23-31)

Duplicated in `prompt_builder.py` (lines 16-24). Both call `config.setup()` once. The pattern is fine but the global flag is not thread-safe (Streamlit runs in a single thread, so it's OK in practice). **Change:** extract to a shared `config.ensure_setup()` and delete both copies.

#### 14. `generate_with_retries` only retries on ConnectionError/Timeout (line 269)

HTTP 500s, 429s, and malformed JSON responses are not retried. **Change:** retry on `requests.HTTPError` with status >= 500 and on `json.JSONDecodeError`.

## Follow-ups for later reviews

- [ ] `main.py`: confirm how the `analysis` dict is built and whether all keys are populated (finding 8).
- [ ] `main.py`: the `QUERY:` loop (lines 944-953) — does it loop forever if the LLM keeps issuing queries? (Check for a max-iterations guard.)