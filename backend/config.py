"""Configuration loading.

All quantitative assumptions live in configs/*.yaml so they are visible,
version-controlled, and never buried in code. Environment variables override
file values using the APP__SECTION__KEY convention (via python-dotenv).
"""
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "configs"


def load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")
    with path.open() as fh:
        data = yaml.safe_load(fh)
    return data or {}


def load_all() -> dict:
    """Load every configs/*.yaml file into a single dict keyed by filename."""
    merged: dict = {}
    for path in sorted(CONFIG_DIR.glob("*.yaml")):
        merged[path.stem] = load_yaml(path.name)
    return merged
