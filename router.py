"""Router implementation: quota-based load balancing and proxying across workers."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import logging
import random
from typing import Any, Dict, List, Optional, Tuple
from flask import Blueprint, Response, jsonify, request
import requests

from config import WorkerTarget
from fallback import FallbackConfig, FallbackError, call_openai_fallback

logger = logging.getLogger("llm-router.router")


@dataclass
class WorkerStatus:
    target: WorkerTarget
    is_online: bool
    status_code: int = 0
    error: Optional[str] = None
    gemini_weekly: float = 0.0
    gemini_5h: float = 0.0
    claude_weekly: float = 0.0
    claude_5h: float = 0.0
    raw_limits: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.target.clean_host,
            "online": self.is_online,
            "status_code": self.status_code,
            "error": self.error,
            "quotas": {
                "gemini": {
                    "weekly_remaining": self.gemini_weekly,
                    "five_hour_remaining": self.gemini_5h,
                },
                "claude_gpt": {
                    "weekly_remaining": self.claude_weekly,
                    "five_hour_remaining": self.claude_5h,
                },
            },
        }


def poll_worker_limits(worker: WorkerTarget, timeout: float = 4.0) -> WorkerStatus:
    """Query /limits endpoint of a worker target."""
    url = f"{worker.base_url}/limits"
    try:
        resp = requests.get(
            url,
            auth=(worker.user, worker.password),
            timeout=timeout,
            headers={"User-Agent": "llm-router/1.0"}
        )
        if resp.status_code != 200:
            return WorkerStatus(
                target=worker,
                is_online=False,
                status_code=resp.status_code,
                error=f"HTTP {resp.status_code}: {resp.text[:100]}"
            )

        data = resp.json()
        groups = data.get("groups", {})
        gemini_info = groups.get("gemini", {})
        claude_info = groups.get("claude_gpt", {})

        return WorkerStatus(
            target=worker,
            is_online=True,
            status_code=200,
            gemini_weekly=float(gemini_info.get("weekly_remaining", 0.0)),
            gemini_5h=float(gemini_info.get("five_hour_remaining", 0.0)),
            claude_weekly=float(claude_info.get("weekly_remaining", 0.0)),
            claude_5h=float(claude_info.get("five_hour_remaining", 0.0)),
            raw_limits=data
        )
    except requests.exceptions.RequestException as e:
        return WorkerStatus(
            target=worker,
            is_online=False,
            error=str(e)
        )
    except Exception as e:
        return WorkerStatus(
            target=worker,
            is_online=False,
            error=f"Unexpected error: {e}"
        )


def get_model_quota_group(model_name: str) -> str:
    """Determine quota bucket: 'gemini' or 'claude_gpt'."""
    m = (model_name or "").strip().lower()
    if m.startswith("gemini"):
        return "gemini"
    return "claude_gpt"


def select_best_workers(
    statuses: List[WorkerStatus],
    quota_group: str
) -> List[Tuple[WorkerStatus, float]]:
    """Filter and sort workers by availability for the given model group.

    Criteria:
    1. Worker must be online (status_code == 200)
    2. Weekly quota remaining must be > 0.001
    3. Sorted descending by 5-hour remaining quota, with randomized tie-breaking for equal quotas
    """
    eligible: List[Tuple[WorkerStatus, float]] = []

    for ws in statuses:
        if not ws.is_online:
            continue

        if quota_group == "gemini":
            weekly = ws.gemini_weekly
            five_hour = ws.gemini_5h
        else:
            weekly = ws.claude_weekly
            five_hour = ws.claude_5h

        # Must have non-zero weekly quota remaining
        if weekly <= 0.001:
            logger.info(
                "Skipping worker %s: weekly quota exhausted (%.2f%%)",
                ws.target.clean_host, weekly * 100
            )
            continue

        eligible.append((ws, five_hour))

    if not eligible:
        return []

    # Shuffle first so equal/tied quota tiers are distributed evenly
    random.shuffle(eligible)
    # Sort descending by 5-hour quota rounded to 2 decimals (1% tier)
    eligible.sort(key=lambda item: round(item[1], 2), reverse=True)
    return eligible


def create_router_blueprint(
    workers: List[WorkerTarget],
    default_model: str,
    fallback_config: Optional[FallbackConfig] = None,
) -> Blueprint:
    if not default_model or not isinstance(default_model, str) or not default_model.strip():
        raise ValueError("default_model is required and must be a non-empty string")

    clean_default_model = default_model.strip()
    bp = Blueprint("router", __name__, url_prefix="/router")

    def _try_fallback(
        prompt: str,
        fb_config: FallbackConfig,
        quota_group: str,
        model: str,
        statuses: List[WorkerStatus],
        timeout: Optional[int] = None,
        effort: Optional[str] = None,
        worker_errors: Optional[List[str]] = None,
    ):
        """Attempt OpenAI-compatible fallback API call and return Flask response."""
        logger.info(
            "All workers exhausted/failed for group=%s, attempting fallback API (effort=%s)",
            quota_group, effort or fb_config.reasoning_effort
        )
        try:
            result = call_openai_fallback(prompt, fb_config, timeout=timeout, effort=effort)
            flask_resp = jsonify(result)
            flask_resp.headers["X-Routed-Worker"] = "openai-fallback"
            flask_resp.headers["X-Routed-Model-Group"] = "fallback"
            return flask_resp
        except FallbackError as e:
            logger.error("Fallback API also failed: %s", e.message)
            error_response: Dict[str, Any] = {
                "error": "All workers and fallback failed",
                "fallback_error": e.message,
            }
            if worker_errors:
                error_response["worker_errors"] = worker_errors
            else:
                error_response["workers_summary"] = [s.to_dict() for s in statuses]

            return jsonify(error_response), 502

    def _fetch_all_worker_statuses() -> List[WorkerStatus]:
        statuses: List[WorkerStatus] = []
        if not workers:
            return statuses

        max_workers = min(len(workers), 10)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(poll_worker_limits, w): w for w in workers}
            for fut in as_completed(futures):
                statuses.append(fut.result())
        return statuses

    @bp.route("/status", methods=["GET"])
    def router_status():
        statuses = _fetch_all_worker_statuses()
        online_count = sum(1 for s in statuses if s.is_online)
        return jsonify({
            "total_workers": len(workers),
            "online_workers": online_count,
            "default_model": clean_default_model,
            "fallback_configured": fallback_config is not None,
            "fallback_model": fallback_config.model if fallback_config else None,
            "fallback_reasoning_effort": fallback_config.reasoning_effort if fallback_config else None,
            "workers": [s.to_dict() for s in statuses]
        })

    @bp.route("/chat", methods=["POST"])
    def router_chat():
        if not request.is_json:
            return jsonify({"error": "Content-Type must be application/json"}), 400

        body: Dict[str, Any] = request.get_json(silent=True) or {}
        prompt = body.get("prompt")
        if not prompt or not isinstance(prompt, str) or not prompt.strip():
            return jsonify({"error": "Missing or empty 'prompt' field in JSON body"}), 400

        req_model = body.get("model")
        if req_model and isinstance(req_model, str) and req_model.strip():
            model = req_model.strip()
        else:
            model = clean_default_model

        # Ensure forwarded request body explicitly includes the resolved model
        body["model"] = model

        quota_group = get_model_quota_group(model)
        effort = str(body["effort"]).strip() if body.get("effort") else None
        timeout_raw = body.get("timeout", 300)
        try:
            timeout_val = int(timeout_raw)
        except (ValueError, TypeError):
            timeout_val = 300

        # 1. Concurrently poll all workers for fresh limits
        statuses = _fetch_all_worker_statuses()

        # 2. Filter and rank workers
        ranked = select_best_workers(statuses, quota_group)
        if not ranked:
            # No eligible workers — try fallback if configured
            if fallback_config:
                return _try_fallback(
                    prompt, fallback_config, quota_group, model,
                    statuses, timeout=timeout_val, effort=effort
                )

            return jsonify({
                "error": "No available workers with sufficient quota",
                "quota_group": quota_group,
                "model": model,
                "workers_summary": [s.to_dict() for s in statuses]
            }), 503

        # 3. Try routing to the best worker, with automatic fallback to next best on failure
        errors: List[str] = []
        for worker_status, five_hour_rem in ranked:
            target = worker_status.target
            chat_url = f"{target.base_url}/chat"
            logger.info(
                "Routing request (model=%s, group=%s) to %s (5h quota: %.1f%%)",
                model, quota_group, target.clean_host, five_hour_rem * 100
            )

            try:
                resp = requests.post(
                    chat_url,
                    auth=(target.user, target.password),
                    json=body,
                    timeout=timeout_val,
                    headers={"User-Agent": "llm-router/1.0"}
                )

                # If the worker returned a server error (500, 502, 503, 504) or quota exhaustion (429),
                # fail over to the next candidate worker.
                if resp.status_code >= 500 or resp.status_code == 429:
                    err_msg = (
                        f"Worker {target.clean_host} returned HTTP {resp.status_code}: "
                        f"{resp.text[:150]}"
                    )
                    logger.warning(err_msg)
                    errors.append(err_msg)
                    continue

                # Return response immediately if successful or client error (e.g. 400 Bad Request)
                response_data = resp.content
                flask_resp = Response(
                    response_data,
                    status=resp.status_code,
                    mimetype=resp.headers.get("Content-Type", "application/json")
                )
                flask_resp.headers["X-Routed-Worker"] = target.clean_host
                flask_resp.headers["X-Routed-Model-Group"] = quota_group
                flask_resp.headers["X-Routed-5h-Remaining"] = f"{five_hour_rem:.4f}"
                return flask_resp

            except requests.exceptions.RequestException as e:
                err_msg = f"Worker {target.clean_host} network error: {e}"
                logger.warning(err_msg)
                errors.append(err_msg)
                # Fall through to try next candidate worker

        # All eligible workers failed — try fallback if configured
        if fallback_config:
            return _try_fallback(
                prompt, fallback_config, quota_group, model,
                statuses, timeout=timeout_val, effort=effort, worker_errors=errors
            )

        return jsonify({
            "error": "All eligible workers failed to process the request",
            "attempted_errors": errors
        }), 502

    return bp
