"""sqlanvil-sqlx-lint: convention linter for SQLAnvil .sqlx files."""

from .config import Config, DirPolicy, detect_warehouse, load_config, load_config_file
from .linter import Finding, lint_file, lint_text

__all__ = [
    "Config",
    "DirPolicy",
    "Finding",
    "detect_warehouse",
    "lint_file",
    "lint_text",
    "load_config",
    "load_config_file",
]
__version__ = "0.2.0"
