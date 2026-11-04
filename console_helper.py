"""Console color utilities for CLI tools."""

from enum import Enum


class Color(Enum):
    TITLE = 1
    ERROR = 91
    WARNING = 93
    SUCCESS = 92
    MUTED = 90
    CYAN = 96


def color_text(text: str, color: Color) -> None:
    """Print text with ANSI color code."""
    print(f"\033[{color.value}m{text}\033[0m")


def format_color(text: str, color: Color) -> str:
    """Return text formatted with ANSI color code."""
    return f"\033[{color.value}m{text}\033[0m"


def print_ok(text: str) -> None:
    print(f"  {format_color('[OK]', Color.SUCCESS)} {text}")


def print_warn(text: str) -> None:
    print(f"  {format_color('[WARN]', Color.WARNING)} {text}")


def print_err(text: str) -> None:
    print(f"  {format_color('[ERROR]', Color.ERROR)} {text}")
