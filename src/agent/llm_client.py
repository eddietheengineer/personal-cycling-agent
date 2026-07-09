"""
LLM Client for the cycling AI agent.

Supports any OpenAI-compatible API endpoint (vLLM, Ollama, LM Studio,
LocalAI, etc.) via the standard /v1/chat/completions interface.

Configuration via environment variables:
    LLM_BASE_URL   - Base URL (e.g. http://localhost:8010/v1)
    LLM_API_KEY    - API key (optional for local servers)
    LLM_MODEL      - Model name (e.g. qwen3.6-27b)
    LLM_TIMEOUT    - Request timeout in seconds (default 120)

Legacy Ollama endpoint (LLM_ENDPOINT) is still supported for backward
compatibility but will be deprecated.
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
    """Resolve the LLM base URL, preferring OpenAI-compatible format."""
    base_url = os.getenv("LLM_BASE_URL", "")
    if base_url:
        return base_url.rstrip("/")
    # Legacy: LLM_ENDPOINT (Ollama format)
    endpoint = os.getenv("LLM_ENDPOINT", "http://localhost:11434/api/generate")
    if "/v1" in endpoint or "/chat/completions" in endpoint:
        return endpoint
    # Assume Ollama native endpoint
    return endpoint


def _get_model() -> str:
    return os.getenv("LLM_MODEL", "llama3")


def _get_api_key() -> str:
    return os.getenv("LLM_API_KEY", "")


def _get_timeout() -> int:
    return int(os.getenv("LLM_TIMEOUT", "120"))


def _is_openai_compat() -> bool:
    """Detect if we should use OpenAI-compatible API format."""
    base_url = _get_base_url()
    # If LLM_BASE_URL is set or endpoint contains /v1, use OpenAI format
    if os.getenv("LLM_BASE_URL"):
        return True
    if "/v1" in base_url:
        return True
    return False


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


# ── Legacy Ollama API ────────────────────────────────────────────────

def _ollama_generate(prompt: str, stream: bool) -> str:
    """Generate using Ollama's native /api/generate endpoint."""
    endpoint = _get_base_url()
    timeout = _get_timeout()
    model = _get_model()

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "options": {
            "temperature": 0.3,
            "num_predict": 2048,
        },
    }

    logger.info(f"Sending to {endpoint} (model={model})")

    if stream:
        return _ollama_stream(endpoint, payload, timeout)
    else:
        return _ollama_blocking(endpoint, payload, timeout)


def _ollama_blocking(endpoint: str, payload: dict, timeout: int) -> str:
    resp = requests.post(
        endpoint,
        json=payload,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    response = data.get("response", "")
    logger.info(f"Received {len(response)} characters from LLM")
    return response


def _ollama_stream(endpoint: str, payload: dict, timeout: int) -> str:
    full_response = []
    with requests.post(
        endpoint,
        json=payload,
        stream=True,
        timeout=timeout,
        headers={"Content-Type": "application/json"},
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
                token = chunk.get("response", "")
                if token:
                    full_response.append(token)
                    print(token, end="", flush=True)
            except json.JSONDecodeError:
                continue
    print()
    response = "".join(full_response)
    logger.info(f"Received {len(response)} characters from LLM (streamed)")
    return response


# ── Public API ───────────────────────────────────────────────────────

def generate(prompt: str, stream: bool = False) -> str:
    """
    Send a prompt to the LLM and return the response.

    Automatically selects OpenAI-compatible or Ollama native API based
    on configuration.

    Args:
        prompt: The system prompt (typically from prompt_builder.build_system_prompt).
        stream: If True, prints response as it arrives.

    Returns:
        The complete LLM response as a string.
    """
    _ensure_init()

    try:
        if _is_openai_compat():
            return _openai_generate(prompt, stream)
        else:
            return _ollama_generate(prompt, stream)
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