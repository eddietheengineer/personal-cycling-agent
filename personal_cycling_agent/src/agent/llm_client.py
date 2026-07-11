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
    return os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")


def _get_api_key() -> str:
    return os.getenv("LLM_API_KEY", "")


def _get_timeout() -> int:
    return int(os.getenv("LLM_TIMEOUT", "120"))


def _discover_models() -> list[str]:
    """Query /v1/models and return a list of available model IDs."""
    url = _get_base_url() + "/models"
    api_key = _get_api_key()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return [m.get("id", "") for m in data.get("data", [])]
    except Exception:
        pass
    return []


def _get_model() -> str:
    """Get the LLM model, auto-detecting from /v1/models if not set."""
    model = os.getenv("LLM_MODEL", "")
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
    """Generate using OpenAI-compatible /v1/chat/completions API."""
    base_url = _get_base_url()
    url = f"{base_url}/chat/completions"
    api_key = _get_api_key()
    timeout = _get_timeout()
    model = _get_model()

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
        "stream": stream,
    }

    logger.info(f"Sending to {url} (model={model})")

    if stream:
        return _openai_stream(url, headers, payload, timeout)
    else:
        return _openai_blocking(url, headers, payload, timeout)


def _openai_blocking(url: str, headers: dict, payload: dict, timeout: int) -> str:
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
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
                    print(token, end="", flush=True)
            except (json.JSONDecodeError, IndexError, KeyError):
                continue
    print()
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