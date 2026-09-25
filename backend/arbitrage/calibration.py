"""Calibrated model parameters, fitted from live venue data.

``configs/calibration.yaml`` is written by ``scripts/fit_calibration.py``
from timestamped live observations (see
``scripts/collect_latency_snapshots.py``). It records every fitted value
together with its sample size, date range, and method, so a reader can
judge how much to trust it. Small in-session samples are labeled
preliminary, never presented as production-grade.

When the file is absent or invalid, every model falls back to its
documented placeholder and the placeholder labels propagate as before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from backend.config import load_yaml
from backend.errors import ConfigurationError, DataValidationError

CALIBRATION_FILE = "calibration.yaml"

#: Set to any non-empty value to make load_calibration() return None.
#: The test suite sets this so unit tests always exercise the documented
#: placeholder path, regardless of ambient configs/calibration.yaml.
NO_CALIBRATION_ENV = "ODDS_NO_CALIBRATION"


@dataclass(frozen=True)
class LatencyDriftCalibration:
    """Empirical adverse-drift model: expected |mid move| per second."""

    drift_per_second: float
    n_observations: int
    n_markets: int
    start: str  # ISO date of first observation
    end: str  # ISO date of last observation
    preliminary: bool
    method: str


@dataclass(frozen=True)
class KellyCalibration:
    """Win probability for locked arbitrage, from paper-execution outcomes."""

    arbitrage_p_win: float
    n_executions: int
    start: str
    end: str
    preliminary: bool
    method: str


@dataclass(frozen=True)
class Calibration:
    latency_drift: LatencyDriftCalibration | None
    kelly: KellyCalibration | None


def _parse_latency(d: dict[str, Any]) -> LatencyDriftCalibration:
    return LatencyDriftCalibration(
        drift_per_second=float(d["drift_per_second"]),
        n_observations=int(d["n_observations"]),
        n_markets=int(d["n_markets"]),
        start=str(d["start"]),
        end=str(d["end"]),
        preliminary=bool(d.get("preliminary", True)),
        method=str(d.get("method", "")),
    )


def _parse_kelly(d: dict[str, Any]) -> KellyCalibration:
    return KellyCalibration(
        arbitrage_p_win=float(d["arbitrage_p_win"]),
        n_executions=int(d["n_executions"]),
        start=str(d["start"]),
        end=str(d["end"]),
        preliminary=bool(d.get("preliminary", True)),
        method=str(d.get("method", "")),
    )


def load_calibration(path: str | Path | None = None) -> Calibration | None:
    """Load ``configs/calibration.yaml``. Returns None when absent.

    A bare filename resolves under ``configs/``; an absolute path is read
    directly (used by tests with tmp_path fixtures).

    Raises:
        ConfigurationError: if the file exists but is malformed. A bad
            calibration must fail loudly, never silently fall back to a
            placeholder while claiming to be calibrated.
    """
    if os.environ.get(NO_CALIBRATION_ENV):
        return None
    name = str(path) if path else CALIBRATION_FILE
    try:
        raw = _read_mapping(name)
    except ConfigurationError:
        return None
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{name}: top-level mapping expected")
    try:
        latency = _parse_latency(raw["latency_drift"]) if raw.get("latency_drift") else None
        kelly = _parse_kelly(raw["kelly"]) if raw.get("kelly") else None
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name}: malformed calibration: {exc}") from exc
    if latency is None and kelly is None:
        return None
    return Calibration(latency_drift=latency, kelly=kelly)


def _read_mapping(name: str) -> dict[str, Any]:
    """Read a YAML mapping from configs/ or an absolute path.

    Raises:
        ConfigurationError: if the file does not exist.
        DataValidationError: if the file is not a YAML mapping.
    """
    p = Path(name)
    if not p.is_absolute():
        return load_yaml(name)
    if not p.exists():
        raise ConfigurationError(f"Missing config file: {p}")
    with p.open() as fh:
        data: Any = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise DataValidationError(
            f"Config file {p} must contain a YAML mapping, got {type(data).__name__}"
        )
    return data
