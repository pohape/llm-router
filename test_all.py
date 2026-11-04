"""Comprehensive test suite for llm-router using standard library unittest."""

import base64
import json
import unittest
from unittest.mock import MagicMock, patch
from flask import Flask

from auth import setup_auth
from config import AppConfig, WorkerTarget, parse_bool, validate_config_data
from executor import extract_json_payload
from fallback import FallbackConfig, FallbackError, call_openai_fallback
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


class TestFallbackConfig(unittest.TestCase):
    """Group A: Fallback configuration validation."""

    def test_fallback_config_valid(self):
        data = {
            "is_worker": False, "is_router": True, "port": 8741,
            "user": "u", "password": "p",
            "workers": [{"host": "w.example.com", "user": "u", "password": "p"}],
            "fallback": {
                "enabled": True,
                "api_key": "sk-proj-realkey123456789",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-4o-mini",
                "timeout": 120,
                "max_tokens": 16384
            }
        }
        results = validate_config_data(data)
        errors = [msg for lvl, msg in results if lvl == "ERROR"]
        self.assertEqual(errors, [])
        ok_msgs = " ".join(msg for lvl, msg in results if lvl == "OK")
        self.assertIn("Fallback API key", ok_msgs)
        self.assertIn("Fallback model", ok_msgs)

    def test_fallback_config_disabled(self):
        data = {
            "is_worker": True, "is_router": False, "port": 8741,
            "user": "u", "password": "p",
            "fallback": {"enabled": False, "api_key": "sk-unused"}
        }
        results = validate_config_data(data)
        errors = [msg for lvl, msg in results if lvl == "ERROR"]
        self.assertEqual(errors, [])
        ok_msgs = " ".join(msg for lvl, msg in results if lvl == "OK")
        self.assertIn("disabled", ok_msgs.lower())

    def test_fallback_config_missing_section(self):
        data = {
            "is_worker": True, "is_router": False, "port": 8741,
            "user": "u", "password": "p"
        }
        results = validate_config_data(data)
        errors = [msg for lvl, msg in results if lvl == "ERROR"]
        self.assertEqual(errors, [])
        ok_msgs = " ".join(msg for lvl, msg in results if lvl == "OK")
        self.assertIn("No fallback section", ok_msgs)

    def test_fallback_config_enabled_no_key(self):
        data = {
            "is_worker": True, "is_router": False, "port": 8741,
            "user": "u", "password": "p",
            "fallback": {"enabled": True, "api_key": ""}
        }
        results = validate_config_data(data)
        errors = [msg for lvl, msg in results if lvl == "ERROR"]
        self.assertTrue(any("api_key" in err for err in errors))

    def test_fallback_config_placeholder_key(self):
        data = {
            "is_worker": True, "is_router": False, "port": 8741,
            "user": "u", "password": "p",
            "fallback": {"enabled": True, "api_key": "sk-your-api-key-here"}
        }
        results = validate_config_data(data)
        warns = [msg for lvl, msg in results if lvl == "WARN"]
        self.assertTrue(any("placeholder" in w for w in warns))

    def test_fallback_config_defaults(self):
        from config import _parse_fallback_config
        data = {
            "fallback": {
                "enabled": True,
                "api_key": "sk-test-key-1234"
            }
        }
        cfg = _parse_fallback_config(data)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.base_url, "https://api.openai.com/v1")
        self.assertEqual(cfg.model, "gpt-4o-mini")
        self.assertEqual(cfg.timeout, 120)
        self.assertEqual(cfg.max_tokens, 16384)

    def test_fallback_config_null_fields(self):
        """Ensure JSON with explicit null values parses with defaults without crashing."""
        from config import _parse_fallback_config
        data = {
            "fallback": {
                "enabled": True,
                "api_key": "sk-test-key-1234",
                "base_url": None,
                "model": None,
                "timeout": None,
                "max_tokens": None,
            }
        }
        cfg = _parse_fallback_config(data)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.base_url, "https://api.openai.com/v1")
        self.assertEqual(cfg.model, "gpt-4o-mini")
        self.assertEqual(cfg.timeout, 120)
        self.assertEqual(cfg.max_tokens, 16384)


class TestFallbackAPI(unittest.TestCase):
    """Group B: OpenAI-compatible API call mocking."""

    def _make_config(self):
        return FallbackConfig(
            api_key="sk-test-key-1234",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            timeout=30,
            max_tokens=1024
        )

    def test_openai_fallback_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Hello from OpenAI"}}],
            "model": "gpt-4o-mini-2024-07-18",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        }
        with patch("fallback.requests.post", return_value=mock_resp):
            result = call_openai_fallback("Say hello", self._make_config())
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["response"], "Hello from OpenAI")
        self.assertEqual(result["source"], "openai-fallback")
        self.assertEqual(result["model"], "gpt-4o-mini-2024-07-18")
        self.assertIn("usage", result)

    def test_openai_fallback_401(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"error": {"message": "Invalid API key"}}
        mock_resp.text = "Invalid API key"
        with patch("fallback.requests.post", return_value=mock_resp):
            with self.assertRaises(FallbackError) as ctx:
                call_openai_fallback("test", self._make_config())
            self.assertIn("401", ctx.exception.message)

    def test_openai_fallback_429(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {"error": {"message": "Rate limit exceeded"}}
        mock_resp.text = "Rate limit exceeded"
        with patch("fallback.requests.post", return_value=mock_resp):
            with self.assertRaises(FallbackError) as ctx:
                call_openai_fallback("test", self._make_config())
            self.assertIn("429", ctx.exception.message)

    def test_openai_fallback_500(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"error": {"message": "Internal server error"}}
        mock_resp.text = "Internal server error"
        with patch("fallback.requests.post", return_value=mock_resp):
            with self.assertRaises(FallbackError) as ctx:
                call_openai_fallback("test", self._make_config())
            self.assertIn("500", ctx.exception.message)

    def test_openai_fallback_timeout(self):
        import requests as req_lib
        with patch("fallback.requests.post", side_effect=req_lib.exceptions.Timeout("timed out")):
            with self.assertRaises(FallbackError) as ctx:
                call_openai_fallback("test", self._make_config())
            self.assertIn("timed out", ctx.exception.message.lower())
            self.assertEqual(ctx.exception.status_code, 504)

    def test_openai_fallback_connection_error(self):
        import requests as req_lib
        with patch("fallback.requests.post", side_effect=req_lib.exceptions.ConnectionError("refused")):
            with self.assertRaises(FallbackError) as ctx:
                call_openai_fallback("test", self._make_config())
            self.assertIn("connection", ctx.exception.message.lower())

    def test_openai_fallback_malformed_json(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("bad json")
        with patch("fallback.requests.post", return_value=mock_resp):
            with self.assertRaises(FallbackError) as ctx:
                call_openai_fallback("test", self._make_config())
            self.assertIn("invalid JSON", ctx.exception.message)

    def test_openai_fallback_error_string(self):
        """API returned a string error instead of an object."""
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "Custom gateway error string"}
        mock_resp.text = '{"error": "Custom gateway error string"}'
        with patch("fallback.requests.post", return_value=mock_resp):
            with self.assertRaises(FallbackError) as ctx:
                call_openai_fallback("test", self._make_config())
            self.assertIn("Custom gateway error string", ctx.exception.message)

    def test_openai_fallback_null_content(self):
        """API returned message with content: null (e.g. content_filter)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": None}}],
            "model": "gpt-4o-mini",
            "usage": {"total_tokens": 10}
        }
        with patch("fallback.requests.post", return_value=mock_resp):
            result = call_openai_fallback("test", self._make_config())
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["response"], "")

    def test_openai_fallback_effective_timeout(self):
        """Check that request-specific timeout caps fallback timeout."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}}],
            "model": "gpt-4o-mini"
        }
        with patch("fallback.requests.post", return_value=mock_resp) as mock_post:
            call_openai_fallback("test", self._make_config(), timeout=15)
            self.assertEqual(mock_post.call_args.kwargs["timeout"], 15)

    def test_fallback_utf8_cyrillic(self):
        """Russian prompt and Russian response preserve Unicode properly."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "Привет, это ответ на русском!"}}],
            "model": "gpt-4o-mini"
        }
        with patch("fallback.requests.post", return_value=mock_resp):
            res = call_openai_fallback("Привет мир", self._make_config())
        self.assertEqual(res["response"], "Привет, это ответ на русском!")


class TestRouterFallbackIntegration(unittest.TestCase):
    """Group C: Router + fallback integration tests."""

    def _make_fb_config(self):
        return FallbackConfig(
            api_key="sk-test", base_url="https://api.openai.com/v1",
            model="gpt-4o-mini", timeout=30, max_tokens=1024
        )

    def _make_app_with_fallback(self, workers, fallback_config=None):
        app = Flask(__name__)
        bp = create_router_blueprint(workers, fallback_config=fallback_config)
        app.register_blueprint(bp)
        return app

    def test_all_quotas_exhausted_fallback(self):
        """C1: All workers online but weekly=0 → fallback fires."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        w2 = WorkerTarget("w2.example.com", "u", "p")
        app = self._make_app_with_fallback([w1, w2], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=0.0, gemini_5h=0.5)
        s2 = WorkerStatus(target=w2, is_online=True, status_code=200, gemini_weekly=0.0, gemini_5h=0.8)

        fb_result = {"status": "SUCCESS", "response": "From fallback", "source": "openai-fallback", "model": "gpt-4o-mini"}
        with patch("router.poll_worker_limits", side_effect=[s1, s2]):
            with patch("router.call_openai_fallback", return_value=fb_result):
                res = client.post("/router/chat", json={"prompt": "test", "model": "gemini-3.8-flash-high"})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.get_json()["source"], "openai-fallback")
                self.assertEqual(res.headers.get("X-Routed-Worker"), "openai-fallback")
                self.assertEqual(res.headers.get("X-Routed-Model-Group"), "fallback")

    def test_half_offline_half_exhausted_fallback(self):
        """C2: 1 offline + 1 online with weekly=0 → fallback fires."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        w2 = WorkerTarget("w2.example.com", "u", "p")
        app = self._make_app_with_fallback([w1, w2], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=False, error="connection refused")
        s2 = WorkerStatus(target=w2, is_online=True, status_code=200, gemini_weekly=0.0, gemini_5h=0.3)

        fb_result = {"status": "SUCCESS", "response": "Fallback OK", "source": "openai-fallback"}
        with patch("router.poll_worker_limits", side_effect=[s1, s2]):
            with patch("router.call_openai_fallback", return_value=fb_result):
                res = client.post("/router/chat", json={"prompt": "test", "model": "gemini-3.8-flash-high"})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.get_json()["source"], "openai-fallback")

    def test_all_offline_fallback(self):
        """C3: All workers offline → fallback fires."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        app = self._make_app_with_fallback([w1], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=False, error="timeout")
        fb_result = {"status": "SUCCESS", "response": "Fallback saved us", "source": "openai-fallback"}
        with patch("router.poll_worker_limits", return_value=s1):
            with patch("router.call_openai_fallback", return_value=fb_result):
                res = client.post("/router/chat", json={"prompt": "test"})
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.get_json()["source"], "openai-fallback")

    def test_all_workers_502_fallback(self):
        """C4: Workers online with quota, but all return 502 → fallback fires."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        app = self._make_app_with_fallback([w1], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.9)
        fb_result = {"status": "SUCCESS", "response": "Fallback after 502", "source": "openai-fallback"}

        with patch("router.poll_worker_limits", return_value=s1):
            mock_worker_resp = MagicMock()
            mock_worker_resp.status_code = 502
            mock_worker_resp.text = "Bad Gateway"
            with patch("requests.post", return_value=mock_worker_resp):
                with patch("router.call_openai_fallback", return_value=fb_result):
                    res = client.post("/router/chat", json={"prompt": "test"})
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.get_json()["source"], "openai-fallback")

    def test_all_workers_429_fallback(self):
        """C5: Workers online with quota, but all return 429 → fallback fires."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        app = self._make_app_with_fallback([w1], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.9)
        fb_result = {"status": "SUCCESS", "response": "Fallback after 429", "source": "openai-fallback"}

        with patch("router.poll_worker_limits", return_value=s1):
            mock_worker_resp = MagicMock()
            mock_worker_resp.status_code = 429
            mock_worker_resp.text = "Quota reached"
            with patch("requests.post", return_value=mock_worker_resp):
                with patch("router.call_openai_fallback", return_value=fb_result):
                    res = client.post("/router/chat", json={"prompt": "test"})
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.get_json()["source"], "openai-fallback")

    def test_no_fallback_configured_503(self):
        """C7: No fallback + all workers dead → 503."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        app = self._make_app_with_fallback([w1], fallback_config=None)
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=0.0, gemini_5h=0.0)
        with patch("router.poll_worker_limits", return_value=s1):
            res = client.post("/router/chat", json={"prompt": "test"})
            self.assertEqual(res.status_code, 503)
            self.assertIn("No available workers", res.get_json()["error"])

    def test_fallback_also_fails_502(self):
        """C8: All workers failed + fallback also fails → 502 with both errors."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        app = self._make_app_with_fallback([w1], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=0.0, gemini_5h=0.0)

        with patch("router.poll_worker_limits", return_value=s1):
            with patch("router.call_openai_fallback", side_effect=FallbackError("OpenAI API returned 429")):
                res = client.post("/router/chat", json={"prompt": "test"})
                self.assertEqual(res.status_code, 502)
                data = res.get_json()
                self.assertIn("fallback", data["error"].lower())
                self.assertIn("429", data["fallback_error"])

    def test_worker_works_no_fallback_called(self):
        """C9: Worker succeeds → fallback NOT called."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        app = self._make_app_with_fallback([w1], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.9)

        with patch("router.poll_worker_limits", return_value=s1):
            mock_worker_resp = MagicMock()
            mock_worker_resp.status_code = 200
            mock_worker_resp.content = b'{"status": "SUCCESS", "response": "From worker"}'
            mock_worker_resp.headers = {"Content-Type": "application/json"}
            with patch("requests.post", return_value=mock_worker_resp):
                with patch("router.call_openai_fallback") as fb_mock:
                    res = client.post("/router/chat", json={"prompt": "test"})
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.get_json()["response"], "From worker")
                    self.assertEqual(res.headers.get("X-Routed-Worker"), "w1.example.com")
                    fb_mock.assert_not_called()

    def test_failover_to_worker2_no_fallback(self):
        """C10: Worker1 fails, worker2 succeeds → fallback NOT called."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        w2 = WorkerTarget("w2.example.com", "u", "p")
        app = self._make_app_with_fallback([w1, w2], self._make_fb_config())
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.95)
        s2 = WorkerStatus(target=w2, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=0.90)

        with patch("router.poll_worker_limits", side_effect=[s1, s2]):
            def mock_post(url, **kwargs):
                m = MagicMock()
                if "w1.example.com" in url:
                    m.status_code = 502
                    m.text = "Bad Gateway"
                    return m
                else:
                    m.status_code = 200
                    m.content = b'{"status": "SUCCESS", "response": "From w2"}'
                    m.headers = {"Content-Type": "application/json"}
                    return m

            with patch("requests.post", side_effect=mock_post):
                with patch("router.call_openai_fallback") as fb_mock:
                    res = client.post("/router/chat", json={"prompt": "test"})
                    self.assertEqual(res.status_code, 200)
                    self.assertEqual(res.headers.get("X-Routed-Worker"), "w2.example.com")
                    fb_mock.assert_not_called()


class TestRouterFallbackEdgeCases(unittest.TestCase):
    """Group D: Edge cases for fallback."""

    def test_router_status_with_fallback(self):
        """D3: /router/status shows fallback_configured: true."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        fb = FallbackConfig(api_key="sk-test")
        app = Flask(__name__)
        bp = create_router_blueprint([w1], fallback_config=fb)
        app.register_blueprint(bp)
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=1.0)
        with patch("router.poll_worker_limits", return_value=s1):
            res = client.get("/router/status")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.get_json()["fallback_configured"])

    def test_router_status_without_fallback(self):
        """D4: /router/status shows fallback_configured: false."""
        w1 = WorkerTarget("w1.example.com", "u", "p")
        app = Flask(__name__)
        bp = create_router_blueprint([w1], fallback_config=None)
        app.register_blueprint(bp)
        client = app.test_client()

        s1 = WorkerStatus(target=w1, is_online=True, status_code=200, gemini_weekly=1.0, gemini_5h=1.0)
        with patch("router.poll_worker_limits", return_value=s1):
            res = client.get("/router/status")
            self.assertEqual(res.status_code, 200)
            self.assertFalse(res.get_json()["fallback_configured"])


if __name__ == "__main__":
    unittest.main()
