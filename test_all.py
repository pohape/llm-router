"""Comprehensive test suite for llm-router using standard library unittest."""

import base64
import json
import unittest
from unittest.mock import MagicMock, patch
from flask import Flask

from auth import setup_auth
from config import AppConfig, WorkerTarget, parse_bool, validate_config_data
from executor import extract_json_payload
from limiter import LimitsTracker, parse_quota_data
from router import (
    WorkerStatus,
    create_router_blueprint,
    get_model_quota_group,
    select_best_workers,
)
from worker import create_worker_blueprint


class TestConfig(unittest.TestCase):
    def test_parse_bool(self):
        self.assertTrue(parse_bool(True))
        self.assertFalse(parse_bool(False))
        self.assertTrue(parse_bool(1))
        self.assertFalse(parse_bool(0))
        self.assertTrue(parse_bool("true"))
        self.assertFalse(parse_bool("False"))
        self.assertTrue(parse_bool("1"))
        self.assertFalse(parse_bool("0"))
        self.assertIsNone(parse_bool("invalid"))
        self.assertIsNone(parse_bool(None))

    def test_validate_config_both_false(self):
        data = {
            "is_worker": False,
            "is_router": False,
            "port": 8741,
            "user": "admin",
            "password": "changeme"
        }
        results = validate_config_data(data)
        errors = [msg for lvl, msg in results if lvl == "ERROR"]
        self.assertTrue(any("At least one mode must be enabled" in err for err in errors))

    def test_validate_config_router_without_workers(self):
        data = {
            "is_worker": False,
            "is_router": True,
            "port": 8741,
            "user": "admin",
            "password": "changeme",
            "workers": []
        }
        results = validate_config_data(data)
        errors = [msg for lvl, msg in results if lvl == "ERROR"]
        self.assertTrue(any("workers" in err.lower() for err in errors))

    def test_clean_host(self):
        w1 = WorkerTarget("https://llm1.example.com/", "u", "p")
        self.assertEqual(w1.clean_host, "llm1.example.com")
        self.assertEqual(w1.base_url, "https://llm1.example.com")

        w2 = WorkerTarget("http://llm2.example.com:8443", "u", "p")
        self.assertEqual(w2.clean_host, "llm2.example.com:8443")
        self.assertEqual(w2.base_url, "https://llm2.example.com:8443")


class TestQuotaParsing(unittest.TestCase):
    def test_parse_quota_data_normal(self):
        sample_json = {
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {"window": "weekly", "remaining_fraction": 0.95, "reset_time": "2026-11-11T00:00:00Z"},
                                {"window": "5h", "remaining_fraction": 1.0, "reset_time": "2026-11-05T00:00:00Z"}
                            ]
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {"window": "weekly", "remaining_fraction": 0.45, "reset_time": "2026-11-11T00:00:00Z"},
                                {"window": "5h", "remaining_fraction": 0.80, "reset_time": "2026-11-05T00:00:00Z"}
                            ]
                        }
                    ]
                }
            }
        }
        parsed = parse_quota_data(sample_json)
        self.assertEqual(parsed["gemini"]["weekly_remaining"], 0.95)
        self.assertEqual(parsed["gemini"]["five_hour_remaining"], 1.0)
        self.assertEqual(parsed["claude_gpt"]["weekly_remaining"], 0.45)
        self.assertEqual(parsed["claude_gpt"]["five_hour_remaining"], 0.80)

    def test_parse_quota_data_malformed(self):
        # Empty or null structures must not crash
        self.assertEqual(parse_quota_data({})["gemini"]["weekly_remaining"], 0.0)
        self.assertEqual(parse_quota_data({"command": None})["gemini"]["weekly_remaining"], 0.0)
        self.assertEqual(parse_quota_data({"command": {"data": None}})["claude_gpt"]["weekly_remaining"], 0.0)


class TestRouterLogic(unittest.TestCase):
    def test_get_model_quota_group(self):
        self.assertEqual(get_model_quota_group("gemini-3.8-flash-high"), "gemini")
        self.assertEqual(get_model_quota_group("Gemini-2.5-pro"), "gemini")
        self.assertEqual(get_model_quota_group("claude-sonnet-4"), "claude_gpt")
        self.assertEqual(get_model_quota_group("gpt-4o"), "claude_gpt")

    def test_select_best_workers_filtering(self):
        w1 = WorkerTarget("llm1.example.com", "u", "p")
        w2 = WorkerTarget("llm2.example.com", "u", "p")
        w3 = WorkerTarget("llm3.example.com", "u", "p")

        # Worker 1: 5h=1.0, but weekly=0.0 (exhausted!)
        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=0.0, gemini_5h=1.0)
        # Worker 2: 5h=0.5, weekly=0.8
        s2 = WorkerStatus(target=w2, is_online=True, status_code=200, gemini_weekly=0.8, gemini_5h=0.5)
        # Worker 3: 5h=0.9, weekly=0.9
        s3 = WorkerStatus(target=w3, is_online=True, status_code=200, gemini_weekly=0.9, gemini_5h=0.9)

        ranked = select_best_workers([s1, s2, s3], quota_group="gemini")
        # s1 must be filtered out because weekly is 0.0
        self.assertEqual(len(ranked), 2)
        # s3 (0.9) must come before s2 (0.5)
        self.assertEqual(ranked[0][0].target.clean_host, "llm3.example.com")
        self.assertEqual(ranked[1][0].target.clean_host, "llm2.example.com")


class TestHTTPAuthAndEndpoints(unittest.TestCase):
    def test_http_auth_and_health(self):
        app = Flask(__name__)
        setup_auth(app, "testuser", "testpass", public_paths={"/health"})

        @app.route("/health", strict_slashes=False)
        def health():
            return {"status": "ok"}

        @app.route("/protected")
        def protected():
            return {"secret": 42}

        client = app.test_client()

        # /health without auth -> 200
        res = client.get("/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["status"], "ok")

        # /health/ with trailing slash -> 200
        res = client.get("/health/")
        self.assertEqual(res.status_code, 200)

        # /protected without auth -> 401
        res = client.get("/protected")
        self.assertEqual(res.status_code, 401)

        # /protected with wrong password -> 401
        bad_auth = base64.b64encode(b"testuser:wrongpass").decode("ascii")
        res = client.get("/protected", headers={"Authorization": f"Basic {bad_auth}"})
        self.assertEqual(res.status_code, 401)

        # /protected with correct auth -> 200
        good_auth = base64.b64encode(b"testuser:testpass").decode("ascii")
        res = client.get("/protected", headers={"Authorization": f"Basic {good_auth}"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["secret"], 42)

    def test_worker_endpoints(self):
        app = Flask(__name__)
        mock_tracker = MagicMock(spec=LimitsTracker)
        mock_tracker.get_limits.return_value = {
            "updated_at": 123456,
            "groups": {"gemini": {"weekly_remaining": 1.0, "five_hour_remaining": 1.0}}
        }

        bp = create_worker_blueprint(tracker=mock_tracker, agy_binary="/bin/echo")
        app.register_blueprint(bp)
        client = app.test_client()

        # GET /limits -> returns mocked limits
        res = client.get("/limits")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["updated_at"], 123456)

        # POST /chat without JSON body -> 400
        res = client.post("/chat", data="not json", content_type="text/plain")
        self.assertEqual(res.status_code, 400)

        # POST /chat with empty prompt -> 400
        res = client.post("/chat", json={"prompt": ""})
        self.assertEqual(res.status_code, 400)

    def test_extract_json_payload(self):
        # Plain json
        self.assertEqual(extract_json_payload('{"a": 1}'), {"a": 1})

        # CLI output with error lines before JSON
        dirty = """error: Individual quota reached.
AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED"}
{"conversation_id":"xyz","status":"ERROR","error":"quota reached"}"""
        parsed = extract_json_payload(dirty)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["conversation_id"], "xyz")
        self.assertEqual(parsed["status"], "ERROR")

    def test_router_failover(self):
        app = Flask(__name__)
        w1 = WorkerTarget("worker1.example.com", "u", "p")
        w2 = WorkerTarget("worker2.example.com", "u", "p")

        bp = create_router_blueprint([w1, w2])
        app.register_blueprint(bp)
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.9)
        s2 = WorkerStatus(target=w2, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.8)

        with patch("router.poll_worker_limits", side_effect=[s1, s2]):
            # Simulate worker1 returning 502, and worker2 returning 200
            def mock_post(url, **kwargs):
                mock_resp = MagicMock()
                if "worker1.example.com" in url:
                    mock_resp.status_code = 502
                    mock_resp.text = "Gateway Error"
                    mock_resp.content = b'{"error": "gateway down"}'
                    mock_resp.headers = {"Content-Type": "application/json"}
                    return mock_resp
                else:
                    mock_resp.status_code = 200
                    mock_resp.content = b'{"status": "SUCCESS", "response": "Routed to worker 2"}'
                    mock_resp.headers = {"Content-Type": "application/json"}
                    return mock_resp

            with patch("requests.post", side_effect=mock_post):
                res = client.post("/router/chat", json={"prompt": "test prompt", "model": "gemini-3.8-flash-high"})
                # Should have failed over to worker2 and returned 200!
                self.assertEqual(res.status_code, 200)
                data = res.get_json()
                self.assertEqual(data["response"], "Routed to worker 2")
                self.assertEqual(res.headers.get("X-Routed-Worker"), "worker2.example.com")


    def test_router_failover_429(self):
        app = Flask(__name__)
        w1 = WorkerTarget("worker1.example.com", "u", "p")
        w2 = WorkerTarget("worker2.example.com", "u", "p")

        bp = create_router_blueprint([w1, w2])
        app.register_blueprint(bp)
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.95)
        s2 = WorkerStatus(target=w2, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.90)

        with patch("router.poll_worker_limits", side_effect=[s1, s2]):
            def mock_post(url, **kwargs):
                mock_resp = MagicMock()
                if "worker1.example.com" in url:
                    mock_resp.status_code = 429
                    mock_resp.text = "Quota reached"
                    mock_resp.content = b'{"error": "quota reached"}'
                    mock_resp.headers = {"Content-Type": "application/json"}
                    return mock_resp
                else:
                    mock_resp.status_code = 200
                    mock_resp.content = b'{"status": "SUCCESS", "response": "From worker 2"}'
                    mock_resp.headers = {"Content-Type": "application/json"}
                    return mock_resp

            with patch("requests.post", side_effect=mock_post):
                res = client.post("/router/chat", json={"prompt": "hello", "model": "gemini-3.8-flash-high"})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.get_json()["response"], "From worker 2")
                self.assertEqual(res.headers.get("X-Routed-Worker"), "worker2.example.com")


if __name__ == "__main__":
    unittest.main()
