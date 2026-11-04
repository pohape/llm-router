"""Background limit tracking and caching for agy usage quotas."""

from datetime import datetime, timezone
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Optional

from executor import extract_json_payload

logger = logging.getLogger("llm-router.limiter")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LIMITS_FILE = os.path.join(DATA_DIR, "limits.json")


def parse_quota_data(raw_json: Dict[str, Any]) -> Dict[str, Any]:
    """Extract structured quota fractions and reset times from raw agy output."""
    cmd_obj = raw_json.get("command") or {}
    data_obj = cmd_obj.get("data") or {}
    groups_data = data_obj.get("groups") or []

    parsed_groups: Dict[str, Dict[str, Any]] = {
        "gemini": {
            "name": "Gemini Models",
            "weekly_remaining": 0.0,
            "weekly_reset": None,
            "five_hour_remaining": 0.0,
            "five_hour_reset": None,
        },
        "claude_gpt": {
            "name": "Claude and GPT models",
            "weekly_remaining": 0.0,
            "weekly_reset": None,
            "five_hour_remaining": 0.0,
            "five_hour_reset": None,
        },
    }

    for group in groups_data:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "").lower()
        target_key = "gemini" if "gemini" in group_name else "claude_gpt"

        buckets = group.get("buckets") or []
        for b in buckets:
            if not isinstance(b, dict):
                continue
            window = str(b.get("window") or "").lower()
            try:
                remaining = float(b.get("remaining_fraction") if b.get("remaining_fraction") is not None else 0.0)
            except (ValueError, TypeError):
                remaining = 0.0
            reset_time = b.get("reset_time")

            if window == "weekly":
                parsed_groups[target_key]["weekly_remaining"] = remaining
                parsed_groups[target_key]["weekly_reset"] = reset_time
            elif window == "5h":
                parsed_groups[target_key]["five_hour_remaining"] = remaining
                parsed_groups[target_key]["five_hour_reset"] = reset_time

    return parsed_groups


class LimitsTracker:
    """Manages periodic execution of 'agy -p /usage' and thread-safe caching."""

    def __init__(self, agy_binary: str, interval_seconds: int = 60, storage_path: str = LIMITS_FILE):
        self.agy_binary = agy_binary
        self.interval = interval_seconds
        self.storage_path = storage_path
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._cached_data: Optional[Dict[str, Any]] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Load existing cache from disk if available
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Attempt to read limits.json from disk on startup."""
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    with self._lock:
                        self._cached_data = data
                logger.info("Loaded cached limits from %s", self.storage_path)
            except Exception as e:
                logger.warning("Could not read cached limits file: %s", e)

    def _save_to_disk(self, data: Dict[str, Any]) -> None:
        """Atomically persist limits to disk."""
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            temp_path = f"{self.storage_path}.tmp.{os.getpid()}"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, self.storage_path)
        except Exception as e:
            logger.error("Failed to write limits to %s: %s", self.storage_path, e)

    def fetch_current_limits(self) -> Optional[Dict[str, Any]]:
        """Run 'agy -p /usage --output-format json' and update cache. Thread-safe."""
        # Avoid concurrent executions of agy usage command
        with self._fetch_lock:
            cmd = [self.agy_binary, "-p", "/usage", "--output-format", "json"]
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False
                )
                if res.returncode != 0:
                    logger.warning("agy usage check returned exit code %d: %s", res.returncode, res.stderr.strip())
                    return self.get_limits()

                raw_json = extract_json_payload(res.stdout)
                if not raw_json:
                    logger.warning("Could not extract JSON from agy usage output: %s", res.stdout.strip()[:200])
                    return self.get_limits()
                now = datetime.now(timezone.utc)
                parsed_groups = parse_quota_data(raw_json)

                payload = {
                    "updated_at": int(now.timestamp()),
                    "updated_at_iso": now.isoformat(),
                    "groups": parsed_groups,
                    "raw": raw_json
                }

                with self._lock:
                    self._cached_data = payload

                self._save_to_disk(payload)
                logger.info(
                    "Updated limits: Gemini 5h=%.0f%%/wk=%.0f%% | Claude/GPT 5h=%.0f%%/wk=%.0f%%",
                    parsed_groups["gemini"]["five_hour_remaining"] * 100,
                    parsed_groups["gemini"]["weekly_remaining"] * 100,
                    parsed_groups["claude_gpt"]["five_hour_remaining"] * 100,
                    parsed_groups["claude_gpt"]["weekly_remaining"] * 100
                )
                return payload
            except subprocess.TimeoutExpired:
                logger.warning("agy usage check timed out after 45 seconds")
                return self.get_limits()
            except json.JSONDecodeError as e:
                logger.warning("agy usage output is not valid JSON: %s", e)
                return self.get_limits()
            except Exception as e:
                logger.error("Unexpected error fetching limits: %s", e)
                return self.get_limits()

    def get_limits(self) -> Optional[Dict[str, Any]]:
        """Get the latest cached limits without blocking or executing agy."""
        with self._lock:
            return self._cached_data

    def start(self) -> None:
        """Start the background refresh thread."""
        if self._thread and self._thread.is_alive():
            return

        def _loop():
            # Run initial fetch immediately if no cached data exists
            if not self.get_limits():
                self.fetch_current_limits()

            while not self._stop_event.is_set():
                # Wait for interval or stop event
                if self._stop_event.wait(timeout=self.interval):
                    break
                self.fetch_current_limits()

        self._stop_event.clear()
        self._thread = threading.Thread(target=_loop, name="LimitsTrackerThread", daemon=True)
        self._thread.start()
        logger.info("Limits tracking thread started (interval: %ds)", self.interval)

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("Limits tracking thread stopped")
