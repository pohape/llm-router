"""HTTP Basic Authentication module."""

import hmac
import json
from typing import Optional, Set
from flask import Flask, Response, request


def safe_compare(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def setup_auth(
    app: Flask,
    expected_user: str,
    expected_password: str,
    public_paths: Optional[Set[str]] = None
) -> None:
    """Register before_request hook on Flask app enforcing HTTP Basic Auth."""
    open_paths = public_paths or {"/health"}

    @app.before_request
    def check_http_basic_auth():
        # Exempt public endpoints like /health (handle with and without trailing slash)
        normalized_path = request.path.rstrip("/") or "/"
        if normalized_path in open_paths or request.path in open_paths:
            return None

        auth = request.authorization
        if not auth or auth.type != "basic":
            return Response(
                json.dumps({"error": "authentication required"}),
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="llm-router"'},
                mimetype="application/json"
            )

        user_match = safe_compare(auth.username or "", expected_user)
        pass_match = safe_compare(auth.password or "", expected_password)

        if not (user_match and pass_match):
            return Response(
                json.dumps({"error": "invalid credentials"}),
                status=401,
                headers={"WWW-Authenticate": 'Basic realm="llm-router"'},
                mimetype="application/json"
            )

        return None
