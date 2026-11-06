"""OpenAI-compatible API fallback for when all workers are unavailable."""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger("llm-router.fallback")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 120
VALID_REASONING_EFFORTS = {"low", "medium", "high"}


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
    reasoning_effort: Optional[str] = None
    max_tokens: Optional[int] = None


def normalize_usage(raw_usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize token usage to be dual-compatible with both agy (input/output) and OpenAI (prompt/completion)."""
    if not raw_usage or not isinstance(raw_usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "thinking_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

    try:
        prompt_tokens = int(raw_usage.get("prompt_tokens") or raw_usage.get("input_tokens") or 0)
    except (ValueError, TypeError):
        prompt_tokens = 0

    try:
        completion_tokens = int(raw_usage.get("completion_tokens") or raw_usage.get("output_tokens") or 0)
    except (ValueError, TypeError):
        completion_tokens = 0

    try:
        total_tokens = int(raw_usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    except (ValueError, TypeError):
        total_tokens = prompt_tokens + completion_tokens

    # Extract reasoning / thinking tokens if present
    reasoning_tokens = 0
    details = raw_usage.get("completion_tokens_details")
    if isinstance(details, dict):
        try:
            reasoning_tokens = int(details.get("reasoning_tokens") or 0)
        except (ValueError, TypeError):
            reasoning_tokens = 0
    elif "thinking_tokens" in raw_usage:
        try:
            reasoning_tokens = int(raw_usage.get("thinking_tokens") or 0)
        except (ValueError, TypeError):
            reasoning_tokens = 0

    cached_tokens = 0
    prompt_details = raw_usage.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        try:
            cached_tokens = int(prompt_details.get("cached_tokens") or 0)
        except (ValueError, TypeError):
            cached_tokens = 0

    return {
        # agy / Google Antigravity compatible keys
        "input_tokens": prompt_tokens,
        "cache_read_tokens": cached_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "thinking_tokens": reasoning_tokens,
        # OpenAI standard keys (backward compatibility)
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
    }


def schema_is_strict_compatible(schema: Any) -> bool:
    """OpenAI strict structured output needs every object to list all its properties in `required`
    and to set `additionalProperties: false`. The router refuses any other schema (HTTP 400), so the
    fallback always sends strict: true and its answer is guaranteed to match the schema."""
    if isinstance(schema, list):
        return all(schema_is_strict_compatible(item) for item in schema)
    if not isinstance(schema, dict):
        return True
    if schema.get("type") == "object":
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is not False or set(schema.get("required") or []) != set(props):
            return False
    nested = [v for k, v in schema.items() if k in ("properties", "$defs", "definitions")]
    children = [c for group in nested if isinstance(group, dict) for c in group.values()]
    children += [schema[k] for k in ("items", "anyOf", "oneOf", "allOf") if k in schema]
    return all(schema_is_strict_compatible(c) for c in children)


def call_openai_fallback(
    prompt: str,
    config: FallbackConfig,
    json_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Call OpenAI-compatible chat completions API as a fallback.

    Args:
        prompt: The user prompt to send.
        config: Fallback configuration with API key, base URL, model, etc.
        json_schema: Optional JSON schema of the answer (the same field the agy worker takes); sent as
            structured output, the parsed object comes back in `structured_output`.

    The timeout is config.timeout in full: the fallback has its own time budget, independent of the
    request's `timeout` (which bounds each worker attempt) and of the time already spent on workers.
    Reasoning effort comes only from config: a request's effort targets the agy model variant
    and never carries over to the fallback model.

    Returns:
        Response dict in universal format matching agy worker responses.

    Raises:
        FallbackError: If the API call fails for any reason.
    """
    effective_timeout = config.timeout

    effective_effort: Optional[str] = None
    if config.reasoning_effort:
        clean_cfg = config.reasoning_effort.strip().lower()
        if clean_cfg in VALID_REASONING_EFFORTS:
            effective_effort = clean_cfg

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

    if effective_effort:
        payload["reasoning_effort"] = effective_effort

    if json_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "response",
                "schema": json_schema,
                "strict": True,
            },
        }

    logger.info(
        "Calling fallback API: url=%s, model=%s, prompt_len=%d, timeout=%ds, effort=%s",
        url, config.model, len(prompt), effective_timeout, effective_effort
    )

    start_time = time.time()
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

    duration_seconds = round(time.time() - start_time, 4)

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

    structured_output = None
    if json_schema is not None:
        try:
            structured_output = json.loads(content)
        except ValueError:
            structured_output = None
        if not isinstance(structured_output, dict):
            raise FallbackError(
                f"Fallback API did not return a JSON object for json_schema: {content[:200]!r}",
                status_code=502,
            )

    conv_id = str(data.get("id") or uuid.uuid4())
    normalized_usage = normalize_usage(data.get("usage"))

    logger.info(
        "Fallback API success: model=%s, duration=%.2fs, total_tokens=%d, thinking_tokens=%d",
        data.get("model", config.model),
        duration_seconds,
        normalized_usage["total_tokens"],
        normalized_usage["thinking_tokens"]
    )

    result: Dict[str, Any] = {
        "status": "SUCCESS",
        "response": content,
        "conversation_id": conv_id,
        "duration_seconds": duration_seconds,
        "model": data.get("model", config.model),
        "source": "openai-fallback",
        "usage": normalized_usage,
    }
    if structured_output is not None:
        result["structured_output"] = structured_output
    return result
