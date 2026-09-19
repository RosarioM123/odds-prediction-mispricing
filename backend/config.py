"""Configuration loading.

All quantitative assumptions live in configs/*.yaml so they are visible,
version-controlled, and never buried in code. Environment variables override
file values using the APP__SECTION__KEY convention (via python-dotenv).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from backend.errors import ConfigurationError, DataValidationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "configs"


def load_yaml(name: str) -> dict[str, Any]:
    """Load one ``configs/*.yaml`` file as a string-keyed mapping.

    Raises:
        ConfigurationError: if the file does not exist.
        DataValidationError: if the file does not contain a YAML mapping.
    """
    path = CONFIG_DIR / name
    if not path.exists():
        raise ConfigurationError(f"Missing config file: {path}")
    with path.open() as fh:
        # yaml.safe_load is untyped; the result is genuinely Any.
        data: Any = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DataValidationError(
            f"Config file {path} must contain a YAML mapping, got {type(data).__name__}"
        )
    return data


def load_all() -> dict[str, dict[str, Any]]:
    """Load every configs/*.yaml file into a single dict keyed by filename.

    Raises:
        ConfigurationError: if a config file is missing.
        DataValidationError: if a config file does not contain a mapping.
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        merged[path.stem] = load_yaml(path.name)
    return merged
