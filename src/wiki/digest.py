"""
Wiki Digest — weekly synthesis of wiki activity.

Reads the wiki log to find recent ingest and query activity,
then generates a weekly recap markdown page that summarizes
what was learned, new topics covered, and patterns across
the week's activity.

Usage:
    from src.wiki.digest import generate_digest
    result = generate_digest(week_offset=0)  # current week
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.wiki.engine import (
    all_pages,
    ensure_wiki,
    read_page,
    write_page,
    wiki_path,
    LOG_FILE,
)
from src.wiki.index import append_log_entry, rebuild_index

logger = logging.getLogger(__name__)

DIGEST_PROMPT = """\
You are generating a weekly digest for a personal cycling agent's wiki.

## Wiki Log Entries (This Week)
{log_entries}

## New or Updated Pages
{new_pages}

## Instructions

Write a concise weekly digest in markdown. Include:
1. **What was learned** — summarize the key topics ingested this week
2. **New concepts** — list new concepts/entities added with brief descriptions
3. **Domain breakdown** — how many pages per domain (performance, wellness, health, ml)
4. **Gaps and next steps** — what topics need more sources or expansion
5. **Cross-cutting patterns** — any connections between topics across domains

Keep it under 800 words. Use [[Wiki Page]] links to reference pages.
Write in a conversational but precise tone — like a coach reviewing the week's research.

Return ONLY the markdown content for the digest page. No JSON wrapper.
"""

DIGEST_TEMPLATE = """---
type: synthesis
category: digest
created: {date}
week: {week_label}
---

# {title}

{body}
"""


def generate_digest(week_offset: int = 0) -> dict[str, Any]:
    """
    Generate a weekly digest of wiki activity.

    Args:
        week_offset: 0 for current week, 1 for last week, etc.

    Returns:
        Dict with 'path', 'week_label', 'summary' keys.
    """
    ensure_wiki()

    # Determine week boundaries
    now = datetime.now()
    week_start = now - timedelta(days=now.weekday() + week_offset * 7)
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    week_label = week_start.strftime("%Y-W%W")
    title = f"{week_start.strftime('%B %d')} – {(week_start + timedelta(days=6)).strftime('%B %d, %Y')}: Weekly Digest"

    # Read log entries for this week
    log_entries = _read_weekly_log(week_start, week_end)

    # Find pages created or updated this week
    new_pages = _find_recent_pages(week_start, week_end)

    if not log_entries and not new_pages:
        logger.info(f"No activity this week ({week_label}), skipping digest")
        return {
            "path": None,
            "week_label": week_label,
            "summary": "No wiki activity this week.",
        }

    # Build prompt
    log_text = "\n".join(log_entries) if log_entries else "No log entries this week."

    page_summaries = []
    for p in new_pages[:20]:  # Limit to avoid context overflow
        page_summaries.append(
            f"- [{p['directory']}] {p.get('title', p['slug'])} "
            f"(category: {p.get('meta', {}).get('category', 'unknown')})"
        )
    pages_text = "\n".join(page_summaries) if page_summaries else "No new pages."

    prompt = DIGEST_PROMPT.format(
        log_entries=log_text,
        new_pages=pages_text,
    )

    # Generate digest via LLM
    logger.info(f"Generating weekly digest for {week_label}")
    from src.agent import llm_client

    body = llm_client.generate_with_retries(prompt)

    # Write digest page
    content = DIGEST_TEMPLATE.format(
        date=datetime.now().strftime("%Y-%m-%d"),
        week_label=week_label,
        title=title,
        body=body,
    )

    slug = f"{week_label}-digest"
    path = write_page("syntheses", slug, content)

    # Log the digest
    append_log_entry(
        operation="digest",
        description=f"Weekly digest: {week_label}",
        details=f"Written to syntheses/{slug}. {len(log_entries)} log entries, {len(new_pages)} pages.",
    )

    # Rebuild index
    rebuild_index()

    logger.info(f"Digest written to {path}")
    return {
        "path": str(path),
        "week_label": week_label,
        "summary": body[:300] + "..." if len(body) > 300 else body,
    }


def _read_weekly_log(
    week_start: datetime, week_end: datetime
) -> list[str]:
    """Read log entries within the given week."""
    log_path = wiki_path() / LOG_FILE
    if not log_path.exists():
        return []

    content = log_path.read_text(encoding="utf-8")
    entries = []

    # Parse log entries: ## [YYYY-MM-DD HH:MM] operation | description
    entry_pattern = re.compile(
        r"## \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (\w+) \| (.+)"
    )

    for match in entry_pattern.finditer(content):
        timestamp_str, operation, description = match.groups()
        try:
            timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M")
        except ValueError:
            continue

        if week_start <= timestamp < week_end:
            entries.append(f"- [{timestamp_str}] {operation}: {description}")

    return entries


def _find_recent_pages(
    week_start: datetime, week_end: datetime
) -> list[dict]:
    """Find pages created or updated within the given week."""
    pages = all_pages()
    recent = []

    for p in pages:
        meta = p.get("meta", {})
        for key in ("updated", "created"):
            date_str = meta.get(key, "")
            if not date_str:
                continue
            try:
                page_date = datetime.strptime(date_str, "%Y-%m-%d")
                if week_start <= page_date < week_end:
                    recent.append(p)
                    break
            except ValueError:
                continue

    return recent


def list_digests() -> list[dict[str, Any]]:
    """List all weekly digest pages."""
    from src.wiki.engine import list_pages

    pages = list_pages("syntheses")
    digests = []
    for p in pages:
        meta = p.get("meta", {})
        if meta.get("category") == "digest":
            digests.append({
                "slug": p["slug"],
                "title": p.get("title", p["slug"]),
                "week": meta.get("week", ""),
                "created": meta.get("created", ""),
                "path": p.get("path", ""),
            })
    digests.sort(key=lambda d: d.get("week", ""), reverse=True)
    return digests