#!/usr/bin/env python3
"""
CLI validation tool for llm-router configuration and system environment.
Inspects config.yaml and reports status with colored console output.
"""

import argparse
import os
import sys
import yaml

from config import DEFAULT_CONFIG_PATH, validate_config_data
from console_helper import Color, color_text, print_err, print_ok, print_warn


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate llm-router config.yaml and environment.")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})"
    )
    args = parser.parse_args()

    color_text("=== LLM-ROUTER CONFIGURATION CHECK ===", Color.TITLE)
    print(f"Config path: {args.config}")
    print()

    if not os.path.exists(args.config):
        print_err(f"Config file not found: {args.config}")
        print()
        color_text("Tip: copy config.example.yaml to config.yaml and fill in your values.", Color.MUTED)
        return 1

    try:
        with open(args.config, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print_err(f"YAML syntax error in {args.config}: {e}")
        return 1
    except Exception as e:
        print_err(f"Cannot read {args.config}: {e}")
        return 1

    if not isinstance(data, dict):
        print_err(f"Configuration file {args.config} must contain a YAML mapping/dictionary (got: {type(data).__name__}).")
        return 1

    results = validate_config_data(data)

    counts = {"OK": 0, "WARN": 0, "ERROR": 0}
    for level, msg in results:
        counts[level] = counts.get(level, 0) + 1
        if level == "OK":
            print_ok(msg)
        elif level == "WARN":
            print_warn(msg)
        elif level == "ERROR":
            print_err(msg)

    print()
    color_text("=== SUMMARY ===", Color.TITLE)
    print_ok(f"Passed checks: {counts['OK']}")
    if counts["WARN"] > 0:
        print_warn(f"Warnings:      {counts['WARN']}")
    if counts["ERROR"] > 0:
        print_err(f"Errors:        {counts['ERROR']}")
    print()

    if counts["ERROR"] > 0:
        color_text("Result: Configuration is INVALID. Please fix the errors above.", Color.ERROR)
        return 1

    color_text("Result: Configuration is VALID and ready for launch.", Color.SUCCESS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
