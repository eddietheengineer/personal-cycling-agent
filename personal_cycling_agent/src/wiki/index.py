"""
Wiki Index and Log — manage index.md and log.md.

index.md: content catalog updated on every ingest.
log.md: chronological append-only activity log.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from src.config import vault_path
from src.wiki.engine import (
    wiki_path,
    ensure_wiki,
    list_pages,
    ENTITIES_DIR,
    CONCEPTS_DIR,
    SOURCES_DIR,
    ANALYSES_DIR,
    SYNTHESSES_DIR,
    INDEX_FILE,
    LOG_FILE,
)

logger = logging.getLogger(__name__)


# ── Index ──────────────────────────────────────────────────────────────

def read_index() -> str:
    """Read the current index.md."""
    path = wiki_path() / INDEX_FILE
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def rebuild_index() -> str:
    """Rebuild index.md from the current state of all wiki pages."""
    root = ensure_wiki()
    index_path = root / INDEX_FILE

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Wiki Index",
        "",
        f"*Auto-generated. Last updated: {now}*",
        "",
    ]

    for directory, heading in [
        (SOURCES_DIR, "Sources"),
        (ENTITIES_DIR, "Entities"),
        (CONCEPTS_DIR, "Concepts"),
        (ANALYSES_DIR, "Analyses"),
        (SYNTHESSES_DIR, "Syntheses"),
    ]:
        lines.append(f"## {heading}")
        pages = list_pages(directory)
        if not pages:
            lines.append(f"_No {heading.lower()} yet._")
        else:
            lines.append("| Page | Category | Updated | Sources |")
            lines.append("|------|----------|---------|---------|")
            for page in pages:
                title = page.get("title", page["slug"])
                category = page.get("category", "")
                updated = page.get("updated", "")
                sources = page.get("sources", "")
                lines.append(f"| [[{title}]] | {category} | {updated} | {sources} |")
        lines.append("")

    content = "\n".join(lines)
    index_path.write_text(content, encoding="utf-8")
    logger.info(f"Rebuilt {INDEX_FILE}")
    return content


# ── Log ────────────────────────────────────────────────────────────────

def read_log() -> str:
    """Read the full log.md."""
    path = wiki_path() / LOG_FILE
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def read_recent_log(n_entries: int = 20) -> str:
    """Return the last n_entries log entries."""
    content = read_log()
    if not content:
        return ""

    entries = re_split_entries(content)
    recent = entries[-n_entries:]
    return "\n".join(recent)


def append_log_entry(operation: str, description: str, details: str = "") -> None:
    """Append a dated entry to the log."""
    ensure_wiki()
    log_path = wiki_path() / LOG_FILE

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"## [{now}] {operation} | {description}\n\n"
    if details:
        entry += details + "\n\n"

    log_path.write_text(log_path.read_text(encoding="utf-8") + entry, encoding="utf-8")
    logger.info(f"Log entry: [{now}] {operation} | {description}")


def re_split_entries(content: str) -> list[str]:
    """Split log content into individual entries."""
    import re
    entries = re.split(r"(?=^## \[)", content, flags=re.MULTILINE)
    return [e.strip() for e in entries if e.strip() and e.strip().startswith("## [")]