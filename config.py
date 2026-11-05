"""Configuration loading, validation, and structures for llm-router."""

from dataclasses import dataclass, field
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple
import yaml

from fallback import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT,
    VALID_REASONING_EFFORTS,
    FallbackConfig,
)

CONFIG_FILENAME = "config.yaml"
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
    default_model: Optional[str] = None
    workers: List[WorkerTarget] = field(default_factory=list)
    fallback: Optional[FallbackConfig] = None


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


def _validate_fallback_config(data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Validate the optional 'fallback' section of the configuration."""
    results: List[Tuple[str, str]] = []

    fallback = data.get("fallback")
    if fallback is None:
        results.append(("OK", "No fallback section configured (OpenAI fallback disabled)."))
        return results

    if not isinstance(fallback, dict):
        results.append(("ERROR", "'fallback' must be a dictionary object."))
        return results

    enabled = parse_bool(fallback.get("enabled"))
    if enabled is None:
        results.append((
            "ERROR",
            "'fallback.enabled' must be true/false, but got: "
            f"{repr(fallback.get('enabled'))}",
        ))
        return results

    if not enabled:
        results.append(("OK", "Fallback is explicitly disabled (enabled: false)."))
        return results

    # Fallback is enabled — validate required fields
    api_key = fallback.get("api_key")
    if not api_key or not isinstance(api_key, str) or not api_key.strip():
        results.append(("ERROR", "'fallback.api_key' is required when fallback is enabled."))
    elif api_key.strip() == "sk-your-api-key-here":
        results.append(("WARN", "'fallback.api_key' contains placeholder value 'sk-your-api-key-here'."))
    else:
        # Show first 8 + last 4 chars for identification
        clean_key = api_key.strip()
        masked = clean_key[:8] + "..." + clean_key[-4:] if len(clean_key) > 16 else "***"
        results.append(("OK", f"Fallback API key: {masked}"))

    raw_base_url = fallback.get("base_url")
    base_url = DEFAULT_BASE_URL if raw_base_url is None else str(raw_base_url).strip()
    if not base_url:
        results.append(("ERROR", "'fallback.base_url' must be a non-empty string."))
    elif not base_url.startswith("https://"):
        results.append(("WARN", f"'fallback.base_url' does not use HTTPS: '{base_url}'"))
    else:
        results.append(("OK", f"Fallback base URL: {base_url}"))

    raw_model = fallback.get("model")
    model = DEFAULT_MODEL if raw_model is None else str(raw_model).strip()
    if model:
        results.append(("OK", f"Fallback model: {model}"))
    else:
        results.append(("WARN", f"'fallback.model' is empty, will use default: {DEFAULT_MODEL}"))

    raw_timeout = fallback.get("timeout")
    timeout = DEFAULT_TIMEOUT if raw_timeout is None else raw_timeout
    if isinstance(timeout, int) and 1 <= timeout <= 600:
        results.append(("OK", f"Fallback timeout: {timeout}s"))
    else:
        results.append(("ERROR", f"'fallback.timeout' must be an integer between 1 and 600 (got: {timeout})"))

    raw_tokens = fallback.get("max_tokens")
    if raw_tokens is not None:
        if isinstance(raw_tokens, int) and 1 <= raw_tokens <= 128000:
            results.append(("OK", f"Fallback max_tokens: {raw_tokens}"))
        else:
            results.append(("ERROR", f"'fallback.max_tokens' must be an integer between 1 and 128000 (got: {raw_tokens})"))
    else:
        results.append(("OK", "Fallback max_tokens: unlimited (model default)"))

    raw_effort = fallback.get("reasoning_effort")
    if raw_effort is not None:
        if isinstance(raw_effort, str) and raw_effort.strip().lower() in VALID_REASONING_EFFORTS:
            results.append(("OK", f"Fallback reasoning_effort: '{raw_effort.strip().lower()}'"))
        else:
            results.append(("ERROR", f"'fallback.reasoning_effort' must be one of {sorted(list(VALID_REASONING_EFFORTS))} (got: {repr(raw_effort)})"))
    else:
        results.append(("OK", "Fallback reasoning_effort: not configured (model default)"))

    return results


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
        default_model = data.get("default_model")
        if not default_model or not isinstance(default_model, str) or not default_model.strip():
            results.append((
                "ERROR",
                "'default_model' is required when is_router is true (e.g. 'gemini-3.8-flash-high').",
            ))
        else:
            results.append(("OK", f"Router default model: '{default_model.strip()}'"))

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

    # 6. Check fallback configuration
    results.extend(_validate_fallback_config(data))

    return results


def _parse_fallback_config(data: Dict[str, Any]) -> Optional[FallbackConfig]:
    """Parse the fallback section from config data, returning None if disabled or missing."""
    fallback = data.get("fallback")
    if not fallback or not isinstance(fallback, dict):
        return None

    enabled = parse_bool(fallback.get("enabled"))
    if not enabled:
        return None

    api_key_val = fallback.get("api_key")
    if not api_key_val or not isinstance(api_key_val, str) or not api_key_val.strip():
        return None

    # Safe parsing of URL and model with fallback to defaults
    raw_base_url = fallback.get("base_url")
    base_url = str(raw_base_url).strip() if raw_base_url else DEFAULT_BASE_URL

    raw_model = fallback.get("model")
    model = str(raw_model).strip() if raw_model else DEFAULT_MODEL

    # Safe parsing of integer values
    raw_timeout = fallback.get("timeout")
    timeout = DEFAULT_TIMEOUT
    if raw_timeout is not None:
        try:
            timeout = int(raw_timeout)
        except (ValueError, TypeError):
            timeout = DEFAULT_TIMEOUT

    raw_max_tokens = fallback.get("max_tokens")
    max_tokens = None
    if raw_max_tokens is not None:
        try:
            val = int(raw_max_tokens)
            if val > 0:
                max_tokens = val
        except (ValueError, TypeError):
            max_tokens = None

    raw_effort = fallback.get("reasoning_effort")
    reasoning_effort = None
    if raw_effort and isinstance(raw_effort, str):
        clean_eff = raw_effort.strip().lower()
        if clean_eff in VALID_REASONING_EFFORTS:
            reasoning_effort = clean_eff

    return FallbackConfig(
        api_key=api_key_val.strip(),
        base_url=base_url,
        model=model,
        timeout=timeout,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
    )


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load and validate configuration, returning AppConfig instance or exiting on fatal error."""
    config_path = path or DEFAULT_CONFIG_PATH

    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Config file not found at: {config_path}\n"
            f"Please copy config.example.yaml to config.yaml and adjust settings."
        )

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ValueError(f"Config file {config_path} is not valid YAML: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"Config file {config_path} must define a YAML dictionary (got: {type(data).__name__})")

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

    raw_default_model = data.get("default_model")
    default_model = str(raw_default_model).strip() if raw_default_model else None

    return AppConfig(
        is_worker=bool(parse_bool(data.get("is_worker"))),
        is_router=bool(parse_bool(data.get("is_router"))),
        port=int(data.get("port", 8741)),
        user=str(data.get("user")),
        password=str(data.get("password")),
        default_model=default_model,
        workers=workers_list,
        fallback=_parse_fallback_config(data),
    )
