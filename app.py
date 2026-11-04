"""Entrypoint for llm-router application."""

import atexit
import logging
import signal
import sys
from typing import Optional
from flask import Flask, jsonify

from auth import setup_auth
from config import AppConfig, load_config, resolve_agy_binary
from limiter import LimitsTracker
from router import create_router_blueprint
from worker import create_worker_blueprint

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("llm-router")


def create_app(config: Optional[AppConfig] = None) -> Flask:
    cfg = config or load_config()

    app = Flask(__name__)

    # Public endpoint (no Basic Auth)
    @app.route("/health", methods=["GET"], strict_slashes=False)
    def health_check():
        return jsonify({
            "status": "ok",
            "is_worker": cfg.is_worker,
            "is_router": cfg.is_router
        })

    # Enforce Basic Auth across all other endpoints
    setup_auth(
        app=app,
        expected_user=cfg.user,
        expected_password=cfg.password,
        public_paths={"/health"}
    )

    tracker: Optional[LimitsTracker] = None

    # Register Worker mode
    if cfg.is_worker:
        agy_bin = resolve_agy_binary()
        if not agy_bin:
            logger.error("Worker mode enabled, but 'agy' binary could not be found.")
            sys.exit(1)

        tracker = LimitsTracker(agy_binary=agy_bin, interval_seconds=60)
        tracker.start()

        worker_bp = create_worker_blueprint(tracker=tracker, agy_binary=agy_bin)
        app.register_blueprint(worker_bp)
        logger.info("Worker endpoints mounted (/limits, /chat)")

        # Graceful shutdown for tracker thread
        def shutdown_tracker():
            if tracker:
                tracker.stop()

        atexit.register(shutdown_tracker)

    # Register Router mode
    if cfg.is_router:
        router_bp = create_router_blueprint(workers=cfg.workers)
        app.register_blueprint(router_bp)
        logger.info(
            "Router endpoints mounted (/router/status, /router/chat) with %d target worker(s)",
            len(cfg.workers)
        )

    return app


def main():
    try:
        config = load_config()
    except Exception as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    app = create_app(config)

    logger.info("Starting llm-router on 127.0.0.1:%d", config.port)
    logger.info("Modes: is_worker=%s, is_router=%s", config.is_worker, config.is_router)

    # Handle termination signals cleanly
    def handle_signal(sig, frame):
        logger.info("Received termination signal %d, exiting...", sig)
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Run on localhost only; Caddy acts as external HTTPS reverse proxy
    app.run(host="127.0.0.1", port=config.port, threaded=True)


if __name__ == "__main__":
    main()
