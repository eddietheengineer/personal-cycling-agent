"""
LLM Client for the cycling AI agent.

Supports any OpenAI-compatible API endpoint (vLLM, Ollama, LM Studio,
LocalAI, etc.) via the standard /v1/chat/completions interface.

Configuration via environment variables:
    LLM_BASE_URL   - Base URL (e.g. http://localhost:8010/v1)
    LLM_API_KEY    - API key (optional for local servers)
    LLM_MODEL      - Model name (auto-detected if blank)
    LLM_TIMEOUT    - Request timeout in seconds (default 120)
"""

import json
import logging
import os

import requests

from src import config
from src.config.constants import HTTP_TIMEOUT_SEC

_initialized = False


def _ensure_init() -> None:
    """Lazily initialize config on first use."""
    global _initialized
    if not _initialized:
        config.setup()
        _initialized = True


logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────

def _get_base_url() -> str:
    """Resolve the LLM base URL (OpenAI-compatible /v1 endpoint)."""
    from src.config.llm_config import get_llm_base_url
    return get_llm_base_url().rstrip("/")


def _get_api_key() -> str:
    from src.config.llm_config import get_llm_api_key
    return get_llm_api_key()


def _get_timeout() -> int:
    from src.config.llm_config import get_llm_timeout
    return get_llm_timeout()


def _discover_models() -> list[str]:
    """Query /v1/models and return a list of available model IDs."""
    url = _get_base_url() + "/models"
    api_key = _get_api_key()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SEC)
        if resp.status_code == 200:
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", [])]
    except Exception:
        logger.debug("Failed to discover LLM models", exc_info=True)
    return []


def _get_model() -> str:
    """Get the LLM model, auto-detecting from /v1/models if not set."""
    from src.config.llm_config import get_llm_model
    model = get_llm_model()
    if model:
        return model
    # Auto-detect: pick first available model
    models = _discover_models()
    if models:
        logger.info(f"Auto-detected LLM model: {models[0]}")
        return models[0]
    return "llama3"


# ── OpenAI-compatible API ────────────────────────────────────────────

def _openai_generate(prompt: str, stream: bool) -> str:
    """Generate using OpenAI-compatible /v1/chat/completions API.

    The ``prompt`` text may contain system instructions followed by a
    "Conversation:" block with USER/ASSISTANT turns.  We split it into
    proper chat messages so the LLM has a ``user`` message to respond to.
    """
    base_url = _get_base_url()
    url = f"{base_url}/chat/completions"
    api_key = _get_api_key()
    timeout = _get_timeout()
    model = _get_model()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # --- Split prompt into proper chat messages ---
    messages = _split_prompt_into_messages(prompt)

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 2048,
        "stream": stream,
    }

    logger.info(f"Sending to {url} (model={model}, messages={len(messages)})")

    if stream:
        return _openai_stream(url, headers, payload, timeout)
    else:
        return _openai_blocking(url, headers, payload, timeout)


def _split_prompt_into_messages(prompt: str) -> list[dict]:
    """Split a combined prompt string into system + chat messages.

    Handles two formats:
    1. ``{system}\n\nConversation:\nUSER: ...\nASSISTANT: ...`` — coach chat
    2. Plain text with no Conversation block — prescription / extraction
    """
    conv_marker = "\nConversation:"
    idx = prompt.find(conv_marker)

    if idx == -1:
        # No conversation block — treat as system + a minimal user prompt
        # to ensure the LLM actually generates a response.
        return [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Please respond."},
        ]

    system = prompt[:idx].strip()
    conv_text = prompt[idx + len(conv_marker):].strip()

    # Parse conversation turns: "USER: ...", "ASSISTANT: ..."
    turns: list[dict] = []
    lines = conv_text.split("\n")
    current_role: str | None = None
    current_lines: list[str] = []

    for line in lines:
        if line.startswith("USER: "):
            if current_role is not None:
                turns.append({"role": current_role, "content": "\n".join(current_lines).strip()})
            current_role = "user"
            current_lines = [line[6:]]
        elif line.startswith("ASSISTANT:"):
            if current_role is not None:
                turns.append({"role": current_role, "content": "\n".join(current_lines).strip()})
            current_role = "assistant"
            content = line[len("ASSISTANT:"):]
            current_lines = [content] if content else []
        else:
            current_lines.append(line)

    # Flush last turn
    if current_role is not None:
        content = "\n".join(current_lines).strip()
        if content:
            turns.append({"role": current_role, "content": content})

    # Build final messages list
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(turns)

    # Guard: ensure there's at least a user message (LLMs won't respond to system-only)
    if not any(m["role"] == "user" for m in messages):
        messages.append({"role": "user", "content": "Please respond."})

    return messages


def _openai_blocking(url: str, headers: dict, payload: dict, timeout: int) -> str:
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        logger.error(f"LLM HTTP error: {exc}. Response body: {resp.text[:500]}")
        raise
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    logger.info(f"Received {len(content)} characters from LLM")
    return content


def _openai_stream(url: str, headers: dict, payload: dict, timeout: int) -> str:
    full_response = []
    with requests.post(url, json=payload, headers=headers, stream=True, timeout=timeout) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            if not line_str.startswith("data: "):
                continue
            data = line_str[len("data: "):]
            if data.strip() == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                token = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if token:
                    full_response.append(token)
                    logger.info(token, extra={"streaming": True})
            except json.JSONDecodeError:
                logger.warning(f"Malformed JSON in stream: {data[:100]}")
            except (IndexError, KeyError):
                continue
    response = "".join(full_response)
    logger.info(f"Received {len(response)} characters from LLM (streamed)")
    return response


# ── Public API ───────────────────────────────────────────────────────

def generate(prompt: str, stream: bool = False) -> str:
    """
    Send a prompt to the LLM and return the response.

    Uses OpenAI-compatible /v1/chat/completions API.

    Args:
        prompt: The system prompt (typically from prompt_builder.build_system_prompt).
        stream: If True, prints response as it arrives.

    Returns:
        The complete LLM response as a string.
    """
    _ensure_init()

    try:
        return _openai_generate(prompt, stream)
    except requests.exceptions.ConnectionError:
        base = _get_base_url()
        logger.error(f"Cannot connect to LLM at {base}")
        raise
    except requests.exceptions.Timeout:
        logger.error(f"LLM request timed out after {_get_timeout()}s")
        raise


def generate_with_retries(prompt: str, max_retries: int = 3) -> str:
    """
    Generate with automatic retries on failure.

    Args:
        prompt: The system prompt.
        max_retries: Number of retry attempts.

    Returns:
        The LLM response string.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return generate(prompt)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = e
            logger.warning(f"LLM attempt {attempt}/{max_retries} failed: {e}")

    raise RuntimeError(
        f"LLM generation failed after {max_retries} attempts: {last_error}"
    )