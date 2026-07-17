"""
Wiki Query — LLM-powered question answering against the wiki.

Reads the index to find relevant pages, then synthesizes answers
with citations. Substantial answers can be filed as analysis pages.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from src.wiki.engine import (
    ensure_wiki,
    read_page,
    search_pages,
    _slug,
    ANALYSES_DIR,
)
from src.wiki.index import read_index, append_log_entry, rebuild_index

logger = logging.getLogger(__name__)

QUERY_PROMPT = """\
You are answering a question using a personal cycling agent's wiki.
The wiki covers four domains: performance, wellness, ML, and health.

## Wiki Index
{index}

## Relevant Pages
{pages}

## Question
{question}

## Instructions

Answer the question using only the wiki content above. Follow these rules:
1. Cite specific wiki pages using [[Page Name]] format
2. If the wiki doesn't contain enough information, say so and suggest what sources to add
3. Be specific about evidence quality — distinguish well-supported claims from speculation
4. For health questions, be cautious and note when professional medical advice is needed
5. For performance questions, reference specific metrics and data when available

Return your answer in markdown format. If the answer is substantial and worth
saving for future reference, also include a "save_as_analysis" field with the
full markdown page content.

Format your response as JSON:
{{
  "answer": "markdown answer with citations",
  "citations": ["[[Page Name]]", ...],
  "confidence": "high|medium|low",
  "save_as_analysis": null or "full markdown analysis page",
  "suggested_sources": ["description of sources that would help answer better"]
}}
"""


def query_wiki(question: str, file_analysis: bool = False) -> dict[str, Any]:
    """
    Query the wiki and return an answer with citations.

    Args:
        question: The user's question
        file_analysis: If True, file substantial answers as analysis pages

    Returns:
        Dict with answer, citations, confidence, and metadata
    """
    ensure_wiki()

    from src.agent import llm_client

    # Step 1: Read index to find relevant pages
    index = read_index()

    # Step 2: Search for relevant pages
    matches = search_pages(question)

    # Step 3: Read top matching pages (limit to avoid context overflow)
    page_contents = []
    for match in matches[:10]:
        directory = match["directory"]
        slug = match["slug"]
        content = read_page(directory, slug)
        if content:
            page_contents.append(f"--- Page: {match.get('title', slug)} ({directory}) ---\n{content}")

    if not page_contents:
        return {
            "answer": "The wiki is currently empty or contains no relevant pages. "
                      "Try ingesting some sources first — articles, research papers, "
                      "notes about training, wellness, health, or ML.",
            "citations": [],
            "confidence": "low",
            "save_as_analysis": None,
            "suggested_sources": [],
        }

    pages_text = "\n\n".join(page_contents)

    # Step 4: Build prompt and query LLM
    prompt = QUERY_PROMPT.format(
        index=index,
        pages=pages_text,
        question=question,
    )

    logger.info(f"Wiki query: {question[:80]}...")
    response = llm_client.generate_with_retries(prompt)

    # Step 5: Parse response
    import json

    # Extract JSON
    json_str = response
    if "```json" in response:
        json_str = response.split("```json")[1].split("```")[0].strip()
    elif "```" in response:
        json_str = response.split("```")[1].split("```")[0].strip()

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning("Failed to parse query JSON, returning raw response")
        result = {
            "answer": response,
            "citations": [],
            "confidence": "medium",
            "save_as_analysis": None,
            "suggested_sources": [],
        }

    # Step 6: Optionally file as analysis
    if file_analysis and result.get("save_as_analysis"):
        slug = _slug(question)
        write_path = write_page_safe(ANALYSES_DIR, slug, result["save_as_analysis"])
        rebuild_index()
        result["filed_as"] = str(write_path)

    # Step 7: Log the query
    append_log_entry(
        operation="query",
        description=question[:100],
        details=f"Confidence: {result.get('confidence', 'unknown')}. "
                f"Citations: {len(result.get('citations', []))} pages.",
    )

    return result


def write_page_safe(directory: str, slug: str, content: str):
    """Write a page, handling slug collisions."""
    from src.wiki.engine import write_page
    path = write_page(directory, slug, content)
    return path


def get_context_for_coach(max_tokens: int = 2000) -> str:
    """
    Get a condensed wiki context block for injecting into the coach's system prompt.
    Returns the most relevant wiki content as a context summary.
    """
    ensure_wiki()
    index = read_index()
    if not index or "_No" in index:
        return ""

    # Truncate index to fit within token budget
    if len(index) > max_tokens:
        # Keep header and first few sections
        lines = index.split("\n")
        truncated = []
        section_count = 0
        for line in lines:
            if line.startswith("## "):
                section_count += 1
                if section_count > 4:
                    truncated.append("... [additional sections truncated]")
                    break
            truncated.append(line)
        return "\n".join(truncated)

    return index