"""Execution of agy CLI prompts via subprocess."""

import json
import logging
import os
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger("llm-router.executor")

# agy runs inside this workspace so it finds the custom agents in
# agy_workspace/.agents/agents/. Both agents declare no built-in tools,
# no default components and no MCP servers, so the model never sees a
# tool to call (no command execution, file edits, search or MCP).
AGY_WORKSPACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agy_workspace")
AGY_TEXT_AGENT = "router-text"
# --json-schema delivers the structured output through agy's `finish` tool,
# so the structured-output agent keeps that single tool and nothing else.
AGY_JSON_AGENT = "router-json"


class AgyExecutionError(Exception):
    def __init__(self, message: str, status_code: int = 500, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def extract_json_payload(text: str) -> Optional[Dict[str, Any]]:
    """Robustly extract JSON dictionary from CLI output that may contain log lines or banners."""
    stripped = text.strip()
    if not stripped:
        return None

    # 1. Try parsing full output
    try:
        res = json.loads(stripped)
        if isinstance(res, dict):
            return res
    except json.JSONDecodeError:
        pass

    # 2. Try parsing single-line JSON from the end (most agy CLI formats emit final turn on last line)
    for line in reversed(stripped.splitlines()):
        line_clean = line.strip()
        if line_clean.startswith("{") and line_clean.endswith("}"):
            try:
                res = json.loads(line_clean)
                if isinstance(res, dict):
                    return res
            except json.JSONDecodeError:
                pass

    # 3. Try finding boundary of outermost object
    last_brace = stripped.rfind("}")
    if last_brace != -1:
        first_brace = stripped.find("{")
        if first_brace != -1 and first_brace < last_brace:
            try:
                res = json.loads(stripped[first_brace:last_brace + 1])
                if isinstance(res, dict):
                    return res
            except json.JSONDecodeError:
                pass

    return None


def execute_prompt(
    agy_binary: str,
    prompt: str,
    model: str,
    output_format: str = "json",
    effort: Optional[str] = None,
    timeout_seconds: int = 300,
    json_schema: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Execute agy CLI with the given prompt and model.

    Returns the parsed JSON response or raises AgyExecutionError.
    """
    if not prompt or not prompt.strip():
        raise AgyExecutionError("Prompt cannot be empty", status_code=400)
    if not model or not model.strip():
        raise AgyExecutionError("Model cannot be empty", status_code=400)

    if json_schema is not None and (not isinstance(json_schema, dict) or json_schema.get("type") != "object"):
        raise AgyExecutionError(
            "'json_schema' must be a JSON object whose root has \"type\": \"object\"",
            status_code=400
        )

    clean_model = model.strip()

    cmd = [
        agy_binary,
        "--agent", AGY_JSON_AGENT if json_schema is not None else AGY_TEXT_AGENT,
        "--model", clean_model,
        "-p", prompt,
        "--output-format", output_format
    ]

    if json_schema is not None:
        cmd.extend(["--json-schema", json.dumps(json_schema)])

    if effort:
        clean_effort = effort.strip().lower()
        if clean_effort in ("low", "medium", "high", "xhigh", "max"):
            cmd.extend(["--effort", clean_effort])

    logger.info("Executing agy: model=%s, prompt_len=%d", model, len(prompt))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            cwd=AGY_WORKSPACE_DIR
        )
    except subprocess.TimeoutExpired:
        logger.error("agy execution timed out after %ds", timeout_seconds)
        raise AgyExecutionError(
            f"Prompt execution timed out after {timeout_seconds} seconds",
            status_code=504
        )
    except Exception as e:
        logger.error("Failed to run agy process: %s", e)
        raise AgyExecutionError(f"Subprocess execution failed: {e}", status_code=500)

    if proc.returncode != 0:
        err_msg = proc.stderr.strip() or proc.stdout.strip() or f"Process failed with exit code {proc.returncode}"
        logger.warning("agy exited with %d: %s", proc.returncode, err_msg)
        raise AgyExecutionError(
            f"agy execution error (code {proc.returncode}): {err_msg}",
            status_code=502,
            details={"returncode": proc.returncode, "stderr": proc.stderr}
        )

    stdout_str = proc.stdout.strip()
    if output_format == "json":
        parsed = extract_json_payload(stdout_str)
        if parsed is not None:
            # Check if agy reported an internal execution error (e.g. quota limit reached)
            if parsed.get("status") == "ERROR":
                err_text = str(parsed.get("error") or "Unknown error returned by agy")
                is_quota = (
                    "quota reached" in err_text.lower()
                    or "resource_exhausted" in err_text.lower()
                    or "code 429" in err_text.lower()
                )
                code = 429 if is_quota else 502
                logger.warning("agy returned ERROR status (code %d): %s", code, err_text)
                raise AgyExecutionError(err_text, status_code=code, details=parsed)

            return parsed

        logger.warning("Could not extract JSON from agy stdout. Raw: %s", stdout_str[:200])
        return {
            "status": "SUCCESS",
            "raw_text": stdout_str,
            "warning": "Could not parse stdout as strict JSON"
        }

    return {
        "status": "SUCCESS",
        "response": stdout_str
    }
