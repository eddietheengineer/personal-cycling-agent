"""Memory journal — persistent markdown log of user facts across sessions."""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _journal_path() -> Path:
    from src.config import vault_path
    return vault_path() / "memory_journal.md"


# ── Read ──────────────────────────────────────────────────────────────

def load_journal() -> str:
    """Return the full journal text, or "" if the file doesn't exist."""
    path = _journal_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def load_recent(n_lines: int = 30) -> str:
    """Return the last *n_lines* non-empty lines of the journal."""
    text = load_journal()
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n_lines:])


# ── Write ─────────────────────────────────────────────────────────────

def append_entry(entry: str) -> None:
    """Append a dated entry to the journal (thread-safe)."""
    path = _journal_path()
    header = f"## {date.today()}\n"
    block = f"{header}{entry}\n"

    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(block)


# ── LLM extraction ────────────────────────────────────────────────────

_EXTRACT_PROMPT = """\
You are a memory extraction assistant. Read the conversation below and \
extract any factual details worth remembering for future coaching sessions.

Categories to look for:
- Injuries or health issues
- Preferences (training style, food, music, etc.)
- Constraints (schedule, travel, work)
- Equipment changes (bike, shoes, gadgets)
- Life events (moving, new job, family)

If nothing noteworthy is present, reply with a single dash on its own line: -

Otherwise, reply with a bullet list, one fact per line, starting with "- ".

---
User: {user_msg}
Assistant: {assistant_msg}
---
"""


def extract_memories(user_msg: str, assistant_msg: str) -> list[str]:
    """Call the LLM to extract memory-worthy facts from a conversation turn.

    Returns a list of extracted bullet points.  If the LLM is unavailable
    or returns nothing useful, returns an empty list.
    """
    prompt = _EXTRACT_PROMPT.format(
        user_msg=user_msg,
        assistant_msg=assistant_msg,
    )

    try:
        from src.agent.llm_client import generate
        raw = generate(prompt)
    except Exception:
        logger.debug("LLM unavailable — skipping memory extraction")
        return []

    bullets: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())

    return bullets