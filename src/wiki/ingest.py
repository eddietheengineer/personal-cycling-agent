"""
Wiki Ingest — LLM-driven source ingestion pipeline.

Takes raw source content and uses the LLM to:
1. Create a source summary page
2. Extract entities and concepts in a separate pass
3. Update the index and log
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from src.wiki.engine import (
    ensure_wiki,
    write_page,
    _slug,
    SOURCES_DIR,
    ENTITIES_DIR,
    CONCEPTS_DIR,
)
from src.wiki.index import rebuild_index, append_log_entry

logger = logging.getLogger(__name__)

# ── Phase 1: Source Summary ────────────────────────────────────────────

SOURCE_PROMPT = """\
You are creating a wiki source summary page. Return ONLY valid JSON (no markdown fences).

## Template
---
type: source
title: <title>
author: <author>
date: <date>
url: <url>
format: <article|paper|book|video|podcast|data|other>
domain: <performance|wellness|ml|health>
ingested: <ISO datetime>
---
# Source: <title>
## Summary (2-3 paragraphs)
## Key Takeaways (bullet list)
## Entities Mentioned (use [[Name]])
## Concepts Discussed (use [[Name]])

## Source
Title: {title}
Author: {author}
URL: {url}
Domain: {domain}
Content:
{content}

Return JSON: {{"content": "<full markdown page>"}}
"""

# ── Phase 2: Extract Entities and Concepts ──────────────────────────────

EXTRACT_PROMPT = """\
You are extracting entities and concepts from a wiki source page.
Return ONLY valid JSON (no markdown fences). Keep content brief.

## Source Page
{source_content}

## Wiki Index
{index}

Return JSON:
{{
  "entities": [{{"name": "<name>", "category": "<cat>", "summary": "<1-2 sentences>"}}],
  "concepts": [{{"name": "<name>", "category": "<cat>", "definition": "<1-2 sentences>", "key_points": ["<point>"]}}]
}}

Categories:
- entities: equipment|person|place|organization|health_condition|other
- concepts: performance|wellness|ml|health|nutrition|recovery|other

Only include items not already in the wiki index. Be selective — only the most important.
"""

# ── Phase 3: Build Pages ───────────────────────────────────────────────

ENTITY_TEMPLATE = """---
type: entity
category: {category}
created: {date}
updated: {date}
sources: ["[[Source: {source}]]"]
---

# {name}

## Summary
{summary}

## Related Concepts
*To be linked.*

## Source History
| Source | Date | Key Information |
|--------|------|----------------|
| [[Source: {source}]] | {date} | Introduced this entity |
"""

CONCEPT_TEMPLATE = """---
type: concept
category: {category}
created: {date}
updated: {date}
sources: ["[[Source: {source}]]"]
confidence: medium
---

# {name}

## Definition
{definition}

## Key Points
{key_points}

## Application
*To be expanded with more sources.*

## Evidence Quality
Based on single source; needs corroboration.

## Related Concepts
*To be linked.*

## Source History
| Source | Date | Key Information |
|--------|------|----------------|
| [[Source: {source}]] | {date} | Introduced this concept |
"""


def ingest_source(
    title: str,
    content: str,
    domain: str = "health",
    author: str = "",
    url: str = "",
    source_format: str = "article",
) -> dict[str, Any]:
    """
    Ingest a source into the wiki using the LLM (three-phase).

    Phase 1: Create source summary page
    Phase 2: Extract entities and concepts
    Phase 3: Write entity/concept pages, update index
    """
    ensure_wiki()

    from src.agent import llm_client
    from src.wiki.index import read_index

    result = {
        "source_slug": "",
        "entities_created": 0,
        "concepts_created": 0,
        "pages_updated": 0,
        "contradictions": [],
    }

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")

    # Truncate content
    max_content_len = 6000
    if len(content) > max_content_len:
        content = content[:max_content_len] + "\n\n... [truncated]"

    # ── Phase 1: Source Summary ─────────────────────────────────────
    logger.info(f"Ingesting source: {title} (domain={domain})")
    prompt = SOURCE_PROMPT.format(
        title=title, author=author, url=url, domain=domain, content=content
    )
    response = llm_client.generate_with_retries(prompt)

    slug = _slug(title)
    source_content = _parse_source_json(response, title, author, url, domain, now)
    write_page(SOURCES_DIR, slug, source_content)
    result["source_slug"] = slug

    # ── Phase 2: Extract Entities and Concepts ──────────────────────
    current_index = read_index()
    prompt = EXTRACT_PROMPT.format(
        source_content=source_content[:3000],
        index=current_index,
    )
    response = llm_client.generate_with_retries(prompt)
    extraction = _parse_extract_json(response)

    # ── Phase 3: Write Pages ────────────────────────────────────────
    entities = extraction.get("entities", [])
    concepts = extraction.get("concepts", [])

    for entity in entities:
        name = entity.get("name", "")
        if name:
            e_slug = _slug(name)
            page = ENTITY_TEMPLATE.format(
                name=name,
                category=entity.get("category", "other"),
                summary=entity.get("summary", ""),
                source=title,
                date=date_str,
            )
            write_page(ENTITIES_DIR, e_slug, page)
    result["entities_created"] = len(entities)

    for concept in concepts:
        name = concept.get("name", "")
        if name:
            c_slug = _slug(name)
            key_points = concept.get("key_points", [])
            kp_text = "\n".join(f"- {p}" for p in key_points) if key_points else "- To be expanded"
            page = CONCEPT_TEMPLATE.format(
                name=name,
                category=concept.get("category", "other"),
                definition=concept.get("definition", ""),
                key_points=kp_text,
                source=title,
                date=date_str,
            )
            write_page(CONCEPTS_DIR, c_slug, page)
    result["concepts_created"] = len(concepts)

    # Update index and log
    rebuild_index()
    append_log_entry(
        operation="ingest",
        description=title,
        details=f"Domain: {domain}. Created {result['entities_created']} entities, "
                f"{result['concepts_created']} concepts.",
    )

    logger.info(f"Ingest complete: {title}")
    return result


def _parse_source_json(response: str, title: str, author: str, url: str, domain: str, now: datetime) -> str:
    """Parse source summary JSON, falling back to template on failure."""
    json_str = response.strip()
    if json_str.startswith("{"):
        try:
            data = json.loads(json_str)
            content = data.get("content", "")
            if content and content.startswith("---"):
                return content
        except json.JSONDecodeError:
            pass

    # Fallback: build minimal page
    return f"""---
type: source
title: {title}
author: {author}
date: {now.strftime('%Y-%m-%d')}
url: {url}
format: paper
domain: {domain}
ingested: {now.isoformat()}
---

# Source: {title}

## Summary
*LLM processing returned unexpected format. Raw content stored below.*

## Raw Content

{response[:2000]}
"""


def _parse_extract_json(response: str) -> dict[str, Any]:
    """Parse extraction JSON, returning empty on failure."""
    json_str = response.strip()
    if json_str.startswith("{"):
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            pass
    return {"entities": [], "concepts": []}


# ── Quick Ingest (no LLM) ──────────────────────────────────────────────

def quick_ingest(
    title: str,
    content: str,
    domain: str = "health",
    author: str = "",
    url: str = "",
) -> str:
    """Quick ingest without LLM processing."""
    ensure_wiki()

    now = datetime.now().isoformat()
    slug = _slug(title)

    page_content = f"""---
type: source
title: {title}
author: {author}
date: {now}
url: {url}
format: note
domain: {domain}
ingested: {now}
---

# Source: {title}

## Summary
*Pending LLM processing.*

## Raw Content

{content}
"""

    write_page(SOURCES_DIR, slug, page_content)
    rebuild_index()
    append_log_entry(
        operation="quick_ingest",
        description=title,
        details=f"Domain: {domain}. Raw content stored.",
    )
    return slug