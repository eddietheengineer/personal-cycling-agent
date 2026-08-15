# Review: `src/wiki/` package

7 modules, ~1,500 lines total. An LLM-maintained markdown wiki ("second brain") under the vault. The LLM writes all content; the user curates sources and asks questions.

## Architecture

- `engine.py` (587 ln): filesystem ops — create/read/write/list pages, frontmatter parse/build, keyword search, seed pages.
- `index.py` (3.6KB): `index.md` / `log.md` management, `rebuild_index()`.
- `ingest.py` (338 ln): 3-phase LLM ingest (source summary → entity/concept extraction → page writes).
- `query.py` (187 ln): LLM question-answering against the wiki with citations.
- `lint.py` (14.6KB): structural health checks (orphans, broken links, stale pages).
- `digest.py` (6.3KB): weekly digest generation.
- `schema.py` (216 ln): the wiki schema text fed to the LLM.

## Findings

### 1. `ingest_source` overwrites existing entity/concept pages (lines 205-234)

`write_page(ENTITIES_DIR, e_slug, page)` — if the entity "Garmin Fenix 8" was already ingested from a previous source, a new ingest with the same name **overwrites** the page. The `ENTITY_TEMPLATE` has a `sources: ["[[Source: {source}]]"]` field (line 94) — but the new page only lists the *current* source, losing all previous sources. **Change:** if the page exists, read it, append the new source to the `sources` list, and merge summaries (or at least preserve the old content).

### 2. `ingest_source` makes 2 blocking LLM calls in the UI thread (visualize.py:2726+)

Phase 1 (source summary) and Phase 2 (extraction) are sequential `generate_with_retries` calls. For a 6,000-char source, this is 2× (prompt + generation) round-trips to the LLM. On a local LLM this can take 30-60 seconds. The UI shows a `st.status` spinner but the main thread is blocked. **Change:** run ingest in a `BackgroundTask` (the infrastructure exists in `tasks/worker.py`).

### 3. Content is truncated to 6,000 chars before the LLM sees it (line 176-178)

`content = content[:max_content_len] + "\n\n... [truncated]"` — a research paper is typically 10,000-50,000 chars. The LLM only sees the first 6,000 (roughly the abstract + intro). Key findings in the results/discussion sections are lost. **Change:** increase the limit (the local LLM's context window is likely 4k-32k tokens) or use a map-reduce approach (summarize in chunks, then merge).

### 4. `search_pages` reads every page's full content on every search (lines 354-393)

`list_pages(directory)` reads all frontmatter, then `read_page(directory, slug)` reads the full content for *every* page in *every* directory. For a wiki with 100 pages, that's 100 file reads per search. **Change:** cache page content in memory (invalidate on write), or index the content at write time.

### 5. `search_pages` keyword scoring is naive (lines 374-382)

`score += 10` for title match, `score += 1` for content match — per term. A page that mentions the term once in a 5,000-char document scores the same as a page where the term is the title. No TF-IDF, no proximity weighting. **Change:** weight by term frequency and document length, or use a proper search library.

### 6. `query_wiki` reads up to 10 full pages into the prompt (lines 84-90)

`for match in matches[:10]: ... page_contents.append(f"--- Page: ... ---\n{content}")` — 10 pages × ~2,000 chars = 20,000 chars of wiki content in the prompt, plus the index, plus the question. This can exceed the local LLM's context window. **Change:** limit to 3-5 pages, or truncate each page to its first N chars (the summary section).

### 7. `get_context_for_coach` truncates by character count, not tokens (lines 172-185)

`if len(index) > max_tokens` — `max_tokens=2000` is treated as a *character* limit, not a token limit. 2,000 characters ≈ 500 tokens. The parameter name is misleading. **Change:** rename to `max_chars` or actually count tokens.

### 8. `get_context_for_coach` is never called (grep confirms no callers)

The function exists to inject wiki context into the coach's system prompt, but `prompt_builder.build_system_prompt` doesn't call it, and `visualize.py` doesn't either. The wiki is **not connected to the coach** — the coach has no access to wiki content. **Change:** either wire it into `build_system_prompt` (add a `wiki_context` parameter) or delete it.

### 9. `_parse_frontmatter` is a naive line parser (lines 326-340)

Splits on `:`, takes the first partition. This breaks on:
- Values containing colons: `url: https://example.com` → `url` = `https` (no, `partition` takes the first `:`, so `url` = `https://example.com` — actually fine)
- Multi-line values: `tags: [a, b, c]` → stored as a single string
- Nested structures: not supported
- `sources: ["[[Source: A]]", "[[Source: B]]"]` → the value contains `: ` which is fine for `partition`, but the list is stored as a raw string

It works for the simple `key: value` format used in the templates, but will silently misparse any page with complex frontmatter. **Change:** use a real YAML parser (`pyyaml` is likely already a dependency) or document the limitation.

### 10. `write_page_safe` in query.py is a no-op wrapper (lines 155-159)

```python
def write_page_safe(directory: str, slug: str, content: str):
    """Write a page, handling slug collisions."""
    from src.wiki.engine import write_page
    path = write_page(directory, slug, content)
    return path
```
The docstring says "handling slug collisions" but it just calls `write_page`, which overwrites. **Change:** implement collision handling (append a suffix) or rename to `write_page` and delete the wrapper.

### 11. `rebuild_index` is called after every ingest and every filed analysis (ingest.py:237, query.py:141)

`rebuild_index` reads all pages and rewrites `index.md`. For a large wiki this is O(n) file reads per ingest. **Change:** incrementally update the index (add the new page's entry) instead of rebuilding.

### 12. `schema.py` is a 200-line string constant (lines 16-211)

The wiki schema is a giant markdown string. It's read by the LLM during ingest/query. This is fine as a pattern, but the string is never validated — a typo in the schema silently changes LLM behavior. **Change:** add a unit test that checks the schema contains the expected section headers.

## Cross-cutting

- The wiki is a self-contained subsystem with its own LLM prompts, filesystem layout, and index. It's well-structured but **disconnected from the rest of the system**: the coach doesn't read it (finding 8), the analytics don't write to it, and the digest is generated on-demand from the UI.
- All LLM calls are blocking and in the UI thread. The `BackgroundTask` infrastructure exists but isn't used for wiki operations.