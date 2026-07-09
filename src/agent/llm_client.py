"""
LLM Client for the cycling AI agent.

Sends prompts to a local LLM endpoint (Ollama by default) and
returns the generated training prescription.
"""

import json
import logging
import os

import requests

from src import config

config.setup()

logger = logging.getLogger(__name__)

LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "http://localhost:11434/api/generate")
LLM_MODEL = os.getenv("LLM_MODEL", "llama3")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "120"))


def generate(prompt: str, stream: bool = False) -> str:
    """
    Send a prompt to the local LLM and return the response.

    Args:
        prompt: The system prompt (typically from prompt_builder.build_system_prompt).
        stream: If True, prints response as it arrives.

    Returns:
        The complete LLM response as a string.
    """
    payload = {
        "model": LLM_MODEL,
        "prompt": prompt,
        "stream": stream,
        "options": {
            "temperature": 0.3,  # low for consistent coaching advice
            "num_predict": 2048,
        },
    }

    logger.info(f"Sending prompt to {LLM_ENDPOINT} (model={LLM_MODEL})")

    try:
        if stream:
            return _stream_generate(payload)
        else:
            return _blocking_generate(payload)
    except requests.exceptions.ConnectionError:
        logger.error(f"Cannot connect to LLM at {LLM_ENDPOINT}. Is Ollama running?")
        raise
    except requests.exceptions.Timeout:
        logger.error(f"LLM request timed out after {LLM_TIMEOUT}s")
        raise


def _blocking_generate(payload: dict) -> str:
    """Non-streaming request to the LLM."""
    resp = requests.post(
        LLM_ENDPOINT,
        json=payload,
        timeout=LLM_TIMEOUT,
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()
    response = data.get("response", "")
    logger.info(f"Received {len(response)} characters from LLM")
    return response


def _stream_generate(payload: dict) -> str:
    """Streaming request to the LLM. Prints tokens as they arrive."""
    full_response = []

    with requests.post(
        LLM_ENDPOINT,
        json=payload,
        stream=True,
        timeout=LLM_TIMEOUT,
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

    print()  # newline after stream
    response = "".join(full_response)
    logger.info(f"Received {len(response)} characters from LLM (streamed)")
    return response


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