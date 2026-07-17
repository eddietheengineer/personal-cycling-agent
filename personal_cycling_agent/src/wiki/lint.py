"""
Wiki Lint — health check for the wiki.

Scans the wiki for structural issues:
- Orphan pages (no incoming links from other pages)
- Stale pages (sources updated but wiki page not)
- Thin pages (placeholder content like "To be expanded")
- Missing cross-references (concepts that should link but don't)
- Broken links (references to pages that don't exist)

Usage:
    from src.wiki.lint import lint_wiki
    issues = lint_wiki()
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.wiki.engine import (
    all_pages,
    ensure_wiki,
    read_page,
    wiki_path,
    LOG_FILE,
)
from src.wiki.index import append_log_entry

logger = logging.getLogger(__name__)

# Pattern for wiki links: [[Page Name]] or [[Source: ...]]
LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")

# Placeholder patterns that indicate thin content
PLACEHOLDER_PATTERNS = [
    r"\*To be expanded",
    r"\*To be linked\.",
    r"\*Needs more sources",
    r"needs corroboration",
    r"single source",
]


class LintIssue:
    """A single lint finding."""

    def __init__(
        self,
        severity: str,  # "error", "warning", "info"
        category: str,  # "orphan", "stale", "thin", "broken_link", "missing_crossref"
        page: str,
        message: str,
        suggestion: str = "",
    ):
        self.severity = severity
        self.category = category
        self.page = page
        self.message = message
        self.suggestion = suggestion

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "page": self.page,
            "message": self.message,
            "suggestion": self.suggestion,
        }

    def __repr__(self) -> str:
        icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(self.severity, "?")
        return f"{icon} [{self.category}] {self.page}: {self.message}"


def lint_wiki() -> list[LintIssue]:
    """Run all lint checks and return a list of issues found."""
    ensure_wiki()
    issues: list[LintIssue] = []

    pages = all_pages()
    if not pages:
        return [LintIssue("info", "empty", "(wiki)", "Wiki is empty", "Ingest some sources first")]

    # Build lookup structures
    page_map = _build_page_map(pages)
    link_graph = _build_link_graph(pages, page_map)

    # Run checks
    issues.extend(_check_orphans(pages, link_graph))
    issues.extend(_check_broken_links(pages, page_map))
    issues.extend(_check_thin_pages(pages))
    issues.extend(_check_stale_pages(pages))
    issues.extend(_check_missing_crossrefs(pages, link_graph))

    # Sort: errors first, then warnings, then info
    severity_order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: (severity_order.get(i.severity, 9), i.category, i.page))

    # Log the lint run
    summary = _format_summary(issues)
    append_log_entry(
        operation="lint",
        description=f"Found {len(issues)} issues",
        details=summary,
    )
    logger.info(f"Lint complete: {len(issues)} issues found")
    return issues

def _build_page_map(pages: list[dict]) -> dict[str, dict]:
    """Build a map of slug -> page info for quick lookup."""
    page_map: dict[str, dict] = {}
    for p in pages:
        slug = p["slug"]
        page_map[slug] = p
        # Derive directory from path (e.g., "wiki/entities/foo.md" -> "entities")
        path_str = p.get("path", "")
        parts = path_str.split("/")
        directory = parts[1] if len(parts) > 1 else "concepts"
        p["_directory"] = directory
        # Also index by directory/slug for source pages
        key = f"{directory}/{slug}"
        page_map[key] = p
        # Index by title for [[Source: Title]] links
        title = p.get("title", slug)
        if directory == "sources":
            page_map[f"Source: {title}"] = p
            page_map[f"source: {title}"] = p
    return page_map


def _build_link_graph(
    pages: list[dict], page_map: dict[str, dict]
) -> dict[str, set[str]]:
    """Build a graph of page -> set of pages that link to it."""
    incoming: dict[str, set[str]] = {p["slug"]: set() for p in pages}

    for p in pages:
        content = read_page(p["_directory"], p["slug"])
        if not content:
            continue
        links = LINK_PATTERN.findall(content)
        for link in links:
            # Resolve link to target slug
            target = _resolve_link(link, page_map)
            if target and target in incoming:
                incoming[target].add(p["slug"])

    return incoming


def _resolve_link(link_text: str, page_map: dict[str, dict]) -> str | None:
    """Resolve a wiki link text to a page slug.
    
    Handles multiple formats:
    - Direct slug match: [[gabbett-tj]]
    - Title with spaces: [[Gabbett TJ]] -> gabbett-tj
    - Author names: [[Alfonso et al.]] -> alfonso-et-al
    - Abbreviations: [[ACWR]] -> acutechronic-workload-ratio
    - Substrings: [[Acute Training Load]] -> acute-training-load-atl
    """
    # Direct match
    if link_text in page_map:
        return page_map[link_text]["slug"]
    
    # Case-insensitive exact match
    lower = link_text.lower().strip()
    for slug, info in page_map.items():
        if slug.lower() == lower:
            return slug
    
    # Normalize link text to slug format and try matching
    normalized = _to_slug(link_text)
    if normalized in page_map:
        return page_map[normalized]["slug"]
    
    # Try matching against title fields
    for slug, info in page_map.items():
        title = (info.get("title", "") or "").lower()
        if title == lower or _to_slug(title) == normalized:
            return slug
    
    # Fuzzy: check if normalized slug is a substring of any page slug
    # e.g., [[ACWR]] matches acutechronic-workload-ratio
    # e.g., [[CTL]] matches chronic-training-load-ctl
    best_match = None
    best_len = 0
    for slug, info in page_map.items():
        if normalized in slug and len(slug) > best_len:
            best_match = slug
            best_len = len(slug)
    if best_match:
        return best_match
    
    # Fuzzy: check if any word in the link appears in the slug
    words = normalized.split("-")
    if len(words) >= 2:
        for slug, info in page_map.items():
            matched = sum(1 for w in words if w and w in slug)
            if matched >= len(words) - 1:  # allow one miss
                return slug
    
    return None


def _to_slug(text: str) -> str:
    """Normalize text to slug format for matching."""
    import re
    s = text.lower().strip()
    # Remove "et al.", "et al"
    s = re.sub(r'\bet\s+al\.?\b', 'et-al', s)
    # Replace colons, apostrophes, hyphens, underscores with spaces (separators)
    s = s.replace(":", " ").replace("'", " ").replace("-", " ").replace("_", " ")
    # Collapse whitespace
    s = re.sub(r'\s+', '-', s.strip())
    # Remove trailing/leading hyphens
    s = s.strip('-')
    return s


def _check_orphans(
    pages: list[dict], link_graph: dict[str, set[str]]
) -> list[LintIssue]:
    """Find pages with no incoming links (not counting sources)."""
    issues = []
    for p in pages:
        if p["_directory"] == "sources":
            continue  # Sources don't need incoming links
        slug = p["slug"]
        incoming = link_graph.get(slug, set())
        if not incoming:
            issues.append(LintIssue(
                severity="warning",
                category="orphan",
                page=f"[{p['_directory']}] {p.get('title', slug)}",
                message="No other pages link to this page",
                suggestion="Add a backlink from a related concept or entity page",
            ))
    return issues


def _check_broken_links(
    pages: list[dict], page_map: dict[str, dict]
) -> list[LintIssue]:
    """Find links that reference pages that don't exist."""
    issues = []
    for p in pages:
        content = read_page(p["_directory"], p["slug"])
        if not content:
            continue
        links = LINK_PATTERN.findall(content)
        for link in links:
            target = _resolve_link(link, page_map)
            if target is None:
                issues.append(LintIssue(
                    severity="error",
                    category="broken_link",
                    page=f"[{p['_directory']}] {p.get('title', p['slug'])}",
                    message=f"Links to non-existent page: [[{link}]]",
                    suggestion="Create the missing page or fix the link reference",
                ))
    return issues


def _check_thin_pages(pages: list[dict]) -> list[LintIssue]:
    """Find pages with placeholder or minimal content."""
    issues = []
    for p in pages:
        content = read_page(p["_directory"], p["slug"])
        if not content:
            continue
        # Skip sources — they're raw content, not wiki pages
        if p["_directory"] == "sources":
            continue
        # Skip analyses and syntheses — they're generated
        if p["_directory"] in ("analyses", "syntheses"):
            continue

        body = content.split("---", 2)[-1] if "---" in content else content
        body_stripped = body.strip()

        # Check for placeholder patterns
        for pattern in PLACEHOLDER_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                issues.append(LintIssue(
                    severity="info",
                    category="thin",
                    page=f"[{p['_directory']}] {p.get('title', p['slug'])}",
                    message="Page contains placeholder content",
                    suggestion="Ingest more sources covering this topic to expand the page",
                ))
                break
        else:
            # Check body length (excluding frontmatter)
            if len(body_stripped) < 100:
                issues.append(LintIssue(
                    severity="info",
                    category="thin",
                    page=f"[{p['_directory']}] {p.get('title', p['slug'])}",
                    message=f"Page body is very short ({len(body_stripped)} chars)",
                    suggestion="Ingest more sources covering this topic",
                ))
    return issues


def _check_stale_pages(pages: list[dict]) -> list[LintIssue]:
    """Find pages whose latest source has been updated more recently than the page."""
    issues = []
    today = datetime.now().date()

    for p in pages:
        if p["_directory"] == "sources":
            continue
        page_updated = p.get("updated", "")
        if not page_updated:
            continue

        # Check if page is older than 30 days
        try:
            updated_date = datetime.strptime(page_updated, "%Y-%m-%d").date()
            days_old = (today - updated_date).days
            if days_old > 30:
                issues.append(LintIssue(
                    severity="info",
                    category="stale",
                    page=f"[{p['_directory']}] {p.get('title', p['slug'])}",
                    message=f"Page not updated in {days_old} days",
                    suggestion="Run /sync to refresh this page from latest sources",
                ))
        except ValueError:
            pass
    return issues


def _check_missing_crossrefs(
    pages: list[dict], link_graph: dict[str, set[str]]
) -> list[LintIssue]:
    """Find concept pages in the same category that don't cross-reference each other."""
    issues = []
    # Group concept pages by category
    concepts_by_category: dict[str, list[dict]] = {}
    for p in pages:
        if p["_directory"] == "concepts":
            cat = p.get("category", "unknown")
            concepts_by_category.setdefault(cat, []).append(p)

    # For each category, check if concepts link to each other
    for cat, concepts in concepts_by_category.items():
        if len(concepts) < 2:
            continue
        for c in concepts:
            content = read_page(c["_directory"], c["slug"])
            if not content:
                continue
            linked_slugs = {
                _resolve_link(link, {p["slug"]: p for p in pages})
                for link in LINK_PATTERN.findall(content)
            }
            linked_slugs.discard(None)

            # Check if this concept links to any sibling concepts
            sibling_slugs = {s["slug"] for s in concepts if s["slug"] != c["slug"]}
            cross_linked = linked_slugs & sibling_slugs
            if not cross_linked:
                issues.append(LintIssue(
                    severity="info",
                    category="missing_crossref",
                    page=f"[concepts] {c.get('title', c['slug'])}",
                    message=f"No cross-references to other {cat} concepts",
                    suggestion="Add [[Related Concept]] links to sibling pages in the same category",
                ))
    return issues


def _format_summary(issues: list[LintIssue]) -> str:
    """Format a one-line summary for the log."""
    by_severity = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        by_severity[i.severity] = by_severity.get(i.severity, 0) + 1
    parts = []
    if by_severity["error"]:
        parts.append(f"{by_severity['error']} errors")
    if by_severity["warning"]:
        parts.append(f"{by_severity['warning']} warnings")
    if by_severity["info"]:
        parts.append(f"{by_severity['info']} info")
    return ", ".join(parts) if parts else "clean"


def lint_wiki_for_display(issues: list[LintIssue] | None = None) -> str:
    """Run lint and return a formatted markdown report for display."""
    if issues is None:
        issues = lint_wiki()

    lines = ["# Wiki Lint Report", f"\n_Run at {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n"]

    # Summary
    by_severity: dict[str, list[LintIssue]] = {}
    for i in issues:
        by_severity.setdefault(i.severity, []).append(i)

    total = len(issues)
    errors = len(by_severity.get("error", []))
    warnings = len(by_severity.get("warning", []))
    info = len(by_severity.get("info", []))

    if total == 0:
        lines.append("## ✅ Clean — no issues found\n")
        return "\n".join(lines)

    lines.append(f"## Summary: {errors} errors, {warnings} warnings, {info} info\n")

    # Group by category
    by_category: dict[str, list[LintIssue]] = {}
    for i in issues:
        by_category.setdefault(i.category, []).append(i)

    for category, cat_issues in by_category.items():
        lines.append(f"### {category.replace('_', ' ').title()} ({len(cat_issues)})\n")
        for i in cat_issues:
            icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}.get(i.severity, "?")
            lines.append(f"- {icon} **{i.page}**: {i.message}")
            if i.suggestion:
                lines.append(f"  - _Suggestion: {i.suggestion}_")
        lines.append("")

    return "\n".join(lines)