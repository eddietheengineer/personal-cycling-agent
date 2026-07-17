"""
Wiki Engine — core operations for creating, reading, and updating wiki pages.

The wiki lives as a directory of markdown files under the vault. This
module provides the filesystem-level operations; the LLM handles content
generation through the ingest module.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from src.config import vault_path

logger = logging.getLogger(__name__)

# Wiki directory layout
WIKI_DIR = "wiki"
ENTITIES_DIR = "entities"
CONCEPTS_DIR = "concepts"
SOURCES_DIR = "sources"
ANALYSES_DIR = "analyses"
SYNTHESSES_DIR = "syntheses"

INDEX_FILE = "index.md"
LOG_FILE = "log.md"


def wiki_path() -> Path:
    """Return the root wiki directory path."""
    return vault_path() / WIKI_DIR


def ensure_wiki() -> Path:
    """Create the wiki directory structure if it doesn't exist."""
    root = wiki_path()
    root.mkdir(parents=True, exist_ok=True)
    for subdir in (ENTITIES_DIR, CONCEPTS_DIR, SOURCES_DIR, ANALYSES_DIR, SYNTHESSES_DIR):
        (root / subdir).mkdir(parents=True, exist_ok=True)
    # Ensure index.md exists
    index = root / INDEX_FILE
    if not index.exists():
        index.write_text(_initial_index(), encoding="utf-8")
    # Ensure log.md exists
    log = root / LOG_FILE
    if not log.exists():
        log.write_text("# Wiki Log\n\n", encoding="utf-8")
    return root


def _initial_index() -> str:
    return """# Wiki Index

*Auto-generated. Updated on every ingest.*

## Sources
_No sources ingested yet._

## Entities
_No entities yet._

## Concepts
_No concepts yet._

## Analyses
_No analyses yet._

## Syntheses
_No syntheses yet._
"""


# ── Page I/O ───────────────────────────────────────────────────────────

def _slug(title: str) -> str:
    """Convert a title to a filesystem-safe slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug.strip("-")
    return slug or "untitled"


def _page_dir(page_type: str) -> str:
    """Map a page type to its directory."""
    mapping = {
        "entity": ENTITIES_DIR,
        "concept": CONCEPTS_DIR,
        "source": SOURCES_DIR,
        "analysis": ANALYSES_DIR,
        "synthesis": SYNTHESSES_DIR,
    }
    return mapping.get(page_type, "concepts")


def read_page(directory: str, slug: str) -> str | None:
    """Read a wiki page by directory and slug. Returns None if not found."""
    path = wiki_path() / directory / f"{slug}.md"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_page(directory: str, slug: str, content: str) -> Path:
    """Write a wiki page. Returns the path written."""
    root = ensure_wiki()
    page_dir = root / directory
    page_dir.mkdir(parents=True, exist_ok=True)
    path = page_dir / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    logger.info(f"Wrote wiki page: {path.relative_to(vault_path())}")
    return path


def list_pages(directory: str) -> list[dict[str, Any]]:
    """List all pages in a directory with frontmatter metadata."""
    root = wiki_path() / directory
    if not root.exists():
        return []
    pages = []
    for md_file in sorted(root.glob("*.md")):
        content = md_file.read_text(encoding="utf-8")
        meta = _parse_frontmatter(content)
        pages.append({
            "slug": md_file.stem,
            "path": str(md_file.relative_to(vault_path())),
            "type": meta.get("type", ""),
            "title": meta.get("title", md_file.stem),
            "created": meta.get("created", ""),
            "updated": meta.get("updated", ""),
            "category": meta.get("category", ""),
        })
    return pages


def all_pages() -> list[dict[str, Any]]:
    """List all wiki pages across all directories."""
    result = []
    for directory in (ENTITIES_DIR, CONCEPTS_DIR, SOURCES_DIR, ANALYSES_DIR, SYNTHESSES_DIR):
        result.extend(list_pages(directory))
    return result


# ── Frontmatter ────────────────────────────────────────────────────────

def _parse_frontmatter(content: str) -> dict[str, str]:
    """Parse YAML-like frontmatter from a markdown page."""
    meta: dict[str, str] = {}
    if not content.startswith("---"):
        return meta
    try:
        end = content.index("---", 3)
        block = content[3:end].strip()
        for line in block.split("\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                meta[key.strip()] = value.strip()
    except ValueError:
        pass
    return meta


def _build_frontmatter(meta: dict[str, str]) -> str:
    """Build a frontmatter block from metadata."""
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines)


# ── Search ─────────────────────────────────────────────────────────────

def search_pages(query: str, directories: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Search wiki pages for a query string.
    Returns matching pages ranked by relevance (simple keyword matching).
    """
    if directories is None:
        directories = [ENTITIES_DIR, CONCEPTS_DIR, SOURCES_DIR, ANALYSES_DIR, SYNTHESSES_DIR]

    query_lower = query.lower()
    terms = query_lower.split()
    results: list[dict[str, Any]] = []

    for directory in directories:
        for page_info in list_pages(directory):
            slug = page_info["slug"]
            content = read_page(directory, slug)
            if content is None:
                continue

            content_lower = content.lower()
            score = 0

            # Title match is worth more
            title = page_info.get("title", "").lower()
            for term in terms:
                if term in title:
                    score += 10
                if term in content_lower:
                    score += 1

            if score > 0:
                results.append({
                    **page_info,
                    "directory": directory,
                    "score": score,
                    "snippet": _extract_snippet(content, terms),
                })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _extract_snippet(content: str, terms: list[str], length: int = 120) -> str:
    """Extract a snippet of content around the first matching term."""
    content_lower = content.lower()
    for term in terms:
        idx = content_lower.find(term)
        if idx >= 0:
            start = max(0, idx - 40)
            end = min(len(content), idx + length)
            snippet = content[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(content):
                snippet = snippet + "..."
            return snippet
    # Fallback: first line of content
    first_line = content.split("\n", 1)[0]
    if len(first_line) > length:
        return first_line[:length] + "..."
    return first_line


# ── Stats ──────────────────────────────────────────────────────────────

def wiki_stats() -> dict[str, Any]:
    """Return statistics about the wiki."""
    stats = {
        "entities": 0,
        "concepts": 0,
        "sources": 0,
        "analyses": 0,
        "syntheses": 0,
        "total_pages": 0,
        "total_size_bytes": 0,
    }
    root = wiki_path()
    if not root.exists():
        return stats

    for directory, key in [
        (ENTITIES_DIR, "entities"),
        (CONCEPTS_DIR, "concepts"),
        (SOURCES_DIR, "sources"),
        (ANALYSES_DIR, "analyses"),
        (SYNTHESSES_DIR, "syntheses"),
    ]:
        dir_path = root / directory
        if dir_path.exists():
            count = len(list(dir_path.glob("*.md")))
            stats[key] = count
            stats["total_pages"] += count
            for f in dir_path.glob("*.md"):
                stats["total_size_bytes"] += f.stat().st_size

    return stats