"""OpenAI-compatible API fallback for when all workers are unavailable."""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("llm-router.fallback")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 120


class FallbackError(Exception):
    """Raised when the fallback API call fails."""

    def __init__(self, message: str, status_code: int = 502, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass
class FallbackConfig:
    """Configuration for OpenAI-compatible API fallback."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: int = DEFAULT_TIMEOUT
    max_tokens: Optional[int] = None


def call_openai_fallback(
    prompt: str,
    config: FallbackConfig,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """Call OpenAI-compatible chat completions API as a fallback.

    Args:
        prompt: The user prompt to send.
        config: Fallback configuration with API key, base URL, model, etc.
        timeout: Optional request-specific timeout in seconds (capped by config.timeout).

    Returns:
        Response dict in a format compatible with worker responses.

    Raises:
        FallbackError: If the API call fails for any reason.
    """
    effective_timeout = config.timeout
    if timeout is not None and isinstance(timeout, (int, float)) and timeout > 0:
        effective_timeout = min(config.timeout, int(timeout))

    url = f"{config.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        "User-Agent": "llm-router/1.0",
    }
    payload: Dict[str, Any] = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if config.max_tokens and config.max_tokens > 0:
        payload["max_tokens"] = config.max_tokens

    logger.info(
        "Calling fallback API: url=%s, model=%s, prompt_len=%d, timeout=%ds",
        url, config.model, len(prompt), effective_timeout
    )

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=effective_timeout)
    except requests.exceptions.Timeout:
        raise FallbackError(
            f"Fallback API timed out after {effective_timeout}s",
            status_code=504
        )
    except requests.exceptions.ConnectionError as e:
        raise FallbackError(
            f"Fallback API connection error: {e}",
            status_code=502
        )
    except requests.exceptions.RequestException as e:
        raise FallbackError(
            f"Fallback API request error: {e}",
            status_code=502
        )

    if resp.status_code != 200:
        # Extract error message cleanly from diverse API error formats
        error_detail = ""
        try:
            err_data = resp.json()
            err_obj = err_data.get("error")
            if isinstance(err_obj, dict):
                error_detail = str(err_obj.get("message") or err_obj)
            elif isinstance(err_obj, str):
                error_detail = err_obj
            else:
                error_detail = resp.text[:200]
        except Exception:
            error_detail = resp.text[:200]

        raise FallbackError(
            f"Fallback API returned HTTP {resp.status_code}: {error_detail}",
            status_code=502 if resp.status_code >= 500 else resp.status_code,
            details={"http_status": resp.status_code, "body": error_detail}
        )

    try:
        data = resp.json()
    except Exception as e:
        raise FallbackError(
            f"Fallback API returned invalid JSON: {e}",
            status_code=502
        )

    try:
        content = data["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise FallbackError(
            f"Fallback API returned unexpected response structure: {e}",
            status_code=502,
            details={"raw_response": data}
        )

    logger.info(
        "Fallback API success: model=%s, tokens=%s",
        data.get("model", config.model),
        data.get("usage", {}).get("total_tokens", "unknown") if isinstance(data.get("usage"), dict) else "unknown"
    )

    return {
        "status": "SUCCESS",
        "response": content,
        "source": "openai-fallback",
        "model": data.get("model", config.model),
        "usage": data.get("usage"),
    }
