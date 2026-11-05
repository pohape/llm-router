"""Worker endpoints: /health, /limits, and /chat."""

import logging
from typing import Any, Dict
from flask import Blueprint, jsonify, request

from executor import AgyExecutionError, execute_prompt
from limiter import LimitsTracker

logger = logging.getLogger("llm-router.worker")


def create_worker_blueprint(tracker: LimitsTracker, agy_binary: str) -> Blueprint:
    bp = Blueprint("worker", __name__)

    @bp.route("/limits", methods=["GET"])
    def get_limits():
        data = tracker.get_limits()
        if not data:
            # If not yet populated by background thread, try fetching once synchronously
            data = tracker.fetch_current_limits()

        if not data:
            return jsonify({
                "error": "Limits data is currently unavailable. Please check agy CLI status."
            }), 503

        return jsonify(data)

    @bp.route("/chat", methods=["POST"])
    def chat():
        if not request.is_json:
            return jsonify({"error": "Content-Type must be application/json"}), 400

        body: Dict[str, Any] = request.get_json(silent=True) or {}
        prompt = body.get("prompt")
        if not prompt or not isinstance(prompt, str) or not prompt.strip():
            return jsonify({"error": "Missing or empty 'prompt' field in JSON body"}), 400

        model = body.get("model")
        if not model or not isinstance(model, str) or not model.strip():
            return jsonify({"error": "Missing or empty 'model' field in JSON body"}), 400

        clean_model = model.strip()
        output_format = str(body.get("output_format") or "json")
        effort = str(body.get("effort")) if body.get("effort") is not None else None
        timeout = body.get("timeout", 300)

        try:
            timeout_val = int(timeout)
        except (ValueError, TypeError):
            timeout_val = 300

        try:
            result = execute_prompt(
                agy_binary=agy_binary,
                prompt=prompt,
                model=clean_model,
                output_format=output_format,
                effort=effort,
                timeout_seconds=timeout_val
            )
            return jsonify(result)
        except AgyExecutionError as e:
            return jsonify({
                "error": e.message,
                "details": e.details
            }), e.status_code
        except Exception as e:
            logger.exception("Unexpected error executing prompt: %s", e)
            return jsonify({"error": f"Internal server error: {e}"}), 500

    return bp
