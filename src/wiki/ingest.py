"""
Wiki Ingest — LLM-driven source ingestion pipeline.

Takes raw source content (text, URL, file path) and uses the LLM to:
1. Create a source summary page
2. Extract and create/update entity pages
3. Extract and create/update concept pages
4. Update the index and log
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.wiki.engine import (
    ensure_wiki,
    write_page,
    read_page,
    _slug,
    all_pages,
    SOURCES_DIR,
    ENTITIES_DIR,
    CONCEPTS_DIR,
)
from src.wiki.index import rebuild_index, append_log_entry
from src.wiki.schema import schema_text

logger = logging.getLogger(__name__)

# ── Ingest Prompts ─────────────────────────────────────────────────────

INGEST_PROMPT = """\
You are the wiki maintainer for a personal cycling agent's knowledge base.
Your job is to ingest a new source and integrate it into the wiki.

## Wiki Schema
{schema}

## Current Wiki Index
{index}

## Source to Ingest
Title: {title}
Domain: {domain}
Content:
{content}

## Instructions

Process this source and return a JSON object with the following structure:
{{
  "source_page": {{
    "slug": "sources/slugified-title",
    "content": "full markdown page following the Source Summary template"
  }},
  "entities": [
    {{
      "name": "Entity Name",
      "category": "health_condition|equipment|person|...",
      "content": "full markdown entity page"
    }}
  ],
  "concepts": [
    {{
      "name": "Concept Name",
      "category": "performance|wellness|ml|health|...",
      "content": "full markdown concept page"
    }}
  ],
  "updates": [
    {{
      "directory": "entities|concepts",
      "slug": "existing-page-slug",
      "content": "updated full markdown page"
    }}
  ],
  "contradictions": [
    "Description of any contradictions found with existing wiki content"
  ]
}}

Rules:
- Focus on the four domains: performance, wellness, ML, health
- For health content, pay special attention to recovery protocols, exercises, and injury prevention
- For wellness content, focus on HRV, sleep, recovery metrics, readiness
- For performance content, focus on training metrics, power analysis, load management
- For ML content, focus on models, features, predictions on personal data
- Create new pages for entities/concepts not yet in the wiki
- List existing pages that need updating in "updates"
- Be thorough but concise in summaries
- Use [[Page Name]] syntax for cross-references
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
    Ingest a source into the wiki using the LLM.

    Returns a dict with:
    - source_slug: the created source page slug
    - entities_created: list of entity names created
    - concepts_created: list of concept names created
    - pages_updated: list of page slugs updated
    - contradictions: list of contradiction descriptions
    """
    ensure_wiki()

    from src.agent import llm_client

    # Get current index for context
    from src.wiki.index import read_index

    current_index = read_index()

    # Truncate content if too long for LLM context
    max_content_len = 12000
    if len(content) > max_content_len:
        content = content[:max_content_len] + "\n\n... [content truncated]"

    prompt = INGEST_PROMPT.format(
        schema=schema_text(),
        index=current_index,
        title=title,
        domain=domain,
        content=content,
    )

    logger.info(f"Ingesting source: {title} (domain={domain})")
    response = llm_client.generate_with_retries(prompt)

    # Parse the LLM response
    result = _parse_ingest_response(response, title)

    # Write pages
    _write_ingest_pages(result)

    # Update index and log
    rebuild_index()
    append_log_entry(
        operation="ingest",
        description=title,
        details=f"Domain: {domain}. Created {result['entities_created']} entities, "
                f"{result['concepts_created']} concepts. Updated {result['pages_updated']} pages.",
    )

    logger.info(f"Ingest complete: {title}")
    return result


def _parse_ingest_response(response: str, title: str) -> dict[str, Any]:
    """Parse the LLM's JSON response from ingest."""
    import json

    result = {
        "source_slug": "",
        "entities_created": 0,
        "concepts_created": 0,
        "pages_updated": 0,
        "contradictions": [],
        "raw_response": response,
    }

    # Extract JSON from response (handle markdown code blocks)
    json_str = response
    if "```json" in response:
        json_str = response.split("```json")[1].split("```")[0].strip()
    elif "```" in response:
        json_str = response.split("```")[1].split("```")[0].strip()

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse ingest JSON for '{title}', storing raw response")
        return result

    # Process source page
    source_page = data.get("source_page", {})
    if source_page.get("content"):
        slug = _slug(title)
        write_page(SOURCES_DIR, slug, source_page["content"])
        result["source_slug"] = slug

    # Process new entities
    entities = data.get("entities", [])
    for entity in entities:
        name = entity.get("name", "")
        if name and entity.get("content"):
            slug = _slug(name)
            write_page(ENTITIES_DIR, slug, entity["content"])
    result["entities_created"] = len(entities)

    # Process new concepts
    concepts = data.get("concepts", [])
    for concept in concepts:
        name = concept.get("name", "")
        if name and concept.get("content"):
            slug = _slug(name)
            write_page(CONCEPTS_DIR, slug, concept["content"])
    result["concepts_created"] = len(concepts)

    # Process updates to existing pages
    updates = data.get("updates", [])
    for update in updates:
        directory = update.get("directory", "")
        slug = update.get("slug", "")
        content = update.get("content", "")
        if directory and slug and content:
            write_page(directory, slug, content)
    result["pages_updated"] = len(updates)

    result["contradictions"] = data.get("contradictions", [])
    return result


def _write_ingest_pages(result: dict[str, Any]) -> None:
    """Write all pages from the ingest result."""
    # Already handled in _parse_ingest_response
    pass


# ── Quick Ingest (no LLM) ──────────────────────────────────────────────

def quick_ingest(
    title: str,
    content: str,
    domain: str = "health",
    author: str = "",
    url: str = "",
) -> str:
    """
    Quick ingest without LLM processing — just save the source as-is.
    Useful for storing raw notes or data for later processing.

    Returns the slug of the created source page.
    """
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
        details=f"Domain: {domain}. Raw content stored, pending LLM processing.",
    )
    return slug