"""Configuration loading, validation, and structures for llm-router."""

from dataclasses import dataclass, field
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

CONFIG_FILENAME = "config.json"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, CONFIG_FILENAME)


@dataclass
class WorkerTarget:
    host: str
    user: str
    password: str

    @property
    def clean_host(self) -> str:
        """Strip http:// or https:// and trailing slashes if accidentally included."""
        h = self.host.strip()
        if h.startswith("https://"):
            h = h[len("https://"):]
        elif h.startswith("http://"):
            h = h[len("http://"):]
        return h.rstrip("/")

    @property
    def base_url(self) -> str:
        """Returns standard https URL."""
        return f"https://{self.clean_host}"


@dataclass
class AppConfig:
    is_worker: bool
    is_router: bool
    port: int
    user: str
    password: str
    workers: List[WorkerTarget] = field(default_factory=list)


def parse_bool(value: Any) -> Optional[bool]:
    """Parse bool from boolean or integer (1/0) or string ('true'/'false')."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "1", "yes"):
            return True
        if v in ("false", "0", "no"):
            return False
    return None


def resolve_agy_binary() -> Optional[str]:
    """Find agy CLI in PATH or standard user location."""
    # 1. Standard PATH
    found = shutil.which("agy")
    if found:
        return found

    # 2. Common fallback location for agy
    user_local = os.path.expanduser("~/.local/bin/agy")
    if os.path.isfile(user_local) and os.access(user_local, os.X_OK):
        return user_local

    return None


def validate_config_data(data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Validate configuration dict.

    Returns list of tuples: (level, message) where level is 'OK', 'WARN', or
    'ERROR'.
    """
    results: List[Tuple[str, str]] = []

    # 1. Check is_worker & is_router
    raw_worker = data.get("is_worker")
    raw_router = data.get("is_router")

    worker_val = parse_bool(raw_worker)
    router_val = parse_bool(raw_router)

    if worker_val is None:
        results.append((
            "ERROR",
            "'is_worker' must be true/false (or 1/0), but got: "
            f"{repr(raw_worker)}",
        ))
    if router_val is None:
        results.append((
            "ERROR",
            "'is_router' must be true/false (or 1/0), but got: "
            f"{repr(raw_router)}",
        ))

    if worker_val is False and router_val is False:
        results.append((
            "ERROR",
            "Both 'is_worker' and 'is_router' are false. At least one mode must"
            " be enabled.",
        ))
    elif worker_val is True and router_val is True:
        results.append((
            "OK",
            "Running in hybrid mode (both Worker and Router are enabled).",
        ))
    elif worker_val is True:
        results.append(
            ("OK", "Role: Worker only (local agy execution enabled).")
        )
    elif router_val is True:
        results.append(
            ("OK", "Role: Router only (load balancing across remote workers).")
        )

    # 2. Check port
    port = data.get("port", 8741)
    if not isinstance(port, int) or port < 1 or port > 65535:
        results.append(
            ("ERROR", f"'port' must be an integer between 1 and 65535 (got: {port})")
        )
    else:
        results.append(("OK", f"Local listening port: {port}"))

    # 3. Check credentials
    user = data.get("user")
    password = data.get("password")
    if not user or not isinstance(user, str) or not user.strip():
        results.append(("ERROR", "'user' must be a non-empty string"))
    if not password or not isinstance(password, str) or not password.strip():
        results.append(("ERROR", "'password' must be a non-empty string"))

    if user == "admin" and password == "changeme":
        results.append((
            "WARN",
            "Default credentials 'admin:changeme' in use. Change them for"
            " production safety.",
        ))
    elif user and password:
        results.append(("OK", f"HTTP Basic Auth user: '{user}'"))

    # 4. Check worker requirements
    if worker_val:
        agy_bin = resolve_agy_binary()
        if not agy_bin:
            results.append((
                "ERROR",
                "Worker mode requires 'agy' CLI, but it was not found in PATH"
                " or ~/.local/bin/agy",
            ))
        else:
            results.append(("OK", f"Found 'agy' CLI binary at: {agy_bin}"))

    # 5. Check router requirements
    if router_val:
        workers = data.get("workers")
        if not isinstance(workers, list) or len(workers) == 0:
            results.append((
                "ERROR",
                "Router mode is enabled, but 'workers' list is missing or"
                " empty.",
            ))
        else:
            results.append(("OK", f"Configured {len(workers)} worker(s)"))
            seen_hosts = set()
            for idx, w in enumerate(workers):
                if not isinstance(w, dict):
                    results.append((
                        "ERROR",
                        f"Worker #{idx + 1} is not a valid dictionary object.",
                    ))
                    continue
                w_host = w.get("host", "")
                w_user = w.get("user", "")
                w_pass = w.get("password", "")

                if not w_host or not isinstance(w_host, str):
                    results.append((
                        "ERROR",
                        f"Worker #{idx + 1} has invalid or missing 'host'.",
                    ))
                else:
                    clean_h = WorkerTarget(w_host, "", "").clean_host
                    if clean_h in seen_hosts:
                        results.append((
                            "WARN",
                            f"Worker #{idx + 1} duplicate host: '{clean_h}'.",
                        ))
                    seen_hosts.add(clean_h)

                    if "example.com" in clean_h:
                        results.append((
                            "WARN",
                            f"Worker #{idx + 1} host uses placeholder domain:"
                            f" '{clean_h}'.",
                        ))

                if not w_user:
                    results.append((
                        "ERROR",
                        f"Worker #{idx + 1} has invalid or missing 'user'.",
                    ))
                if not w_pass:
                    results.append((
                        "ERROR",
                        f"Worker #{idx + 1} has invalid or missing 'password'.",
                    ))

    return results


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load and validate configuration, returning AppConfig instance or exiting on fatal error."""
    config_path = path or DEFAULT_CONFIG_PATH

    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Config file not found at: {config_path}\n"
            f"Please copy config.example.json to config.json and adjust settings."
        )

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Config file {config_path} is not valid JSON: {e}")

    results = validate_config_data(data)
    errors = [msg for lvl, msg in results if lvl == "ERROR"]
    if errors:
        error_lines = "\n".join(f" - {err}" for err in errors)
        raise ValueError(f"Configuration validation failed:\n{error_lines}")

    workers_list: List[WorkerTarget] = []
    if data.get("workers") and isinstance(data["workers"], list):
        for w in data["workers"]:
            if isinstance(w, dict) and w.get("host"):
                workers_list.append(WorkerTarget(
                    host=str(w.get("host")),
                    user=str(w.get("user", "")),
                    password=str(w.get("password", ""))
                ))

    return AppConfig(
        is_worker=bool(parse_bool(data.get("is_worker"))),
        is_router=bool(parse_bool(data.get("is_router"))),
        port=int(data.get("port", 8741)),
        user=str(data.get("user")),
        password=str(data.get("password")),
        workers=workers_list,
    )
