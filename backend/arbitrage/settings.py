"""Typed strategy settings loaded from configs/strategy.yaml.

Every threshold the engine uses lives here, parsed once from the YAML so
detection, sizing, risk, and replay all share one source of truth. The raw
YAML stays the human-editable record; this module only types it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.config import load_yaml
from backend.errors import DataValidationError


@dataclass(frozen=True)
class DetectionSettings:
    min_net_edge: float = 0.005
    min_liquidity_contracts: float = 50.0
    max_book_age_seconds: float = 5.0


@dataclass(frozen=True)
class CrossVenueSettings:
    min_match_confidence: float = 0.85
    min_net_edge: float = 0.01


@dataclass(frozen=True)
class KellySettings:
    fraction: float = 0.5
    assumed_edge_probability: float = 0.55
    probability_is_placeholder: bool = True
    arbitrage_p_win: float = 0.99


@dataclass(frozen=True)
class RiskSettings:
    max_position_per_market: float = 500.0
    max_portfolio_exposure: float = 5000.0
    max_venue_exposure: float = 3000.0
    max_daily_loss: float = 100.0
    max_acceptable_latency_ms: float = 2000.0


@dataclass(frozen=True)
class LatencySettings:
    data_ms: float = 250.0
    processing_ms: float = 50.0
    execution_ms: float = 500.0
    adverse_drift_per_second: float = 0.002
    drift_is_placeholder: bool = True


@dataclass(frozen=True)
class StrategyConfig:
    detection: DetectionSettings
    cross_venue: CrossVenueSettings
    kelly: KellySettings
    risk: RiskSettings
    latency: LatencySettings

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StrategyConfig:
        """Build a StrategyConfig from a parsed ``strategy.yaml`` mapping.

        Raises:
            DataValidationError: if a threshold value cannot be coerced to
                its expected numeric type.
            ConfigurationError: if ``strategy.yaml`` is missing (via load).
        """
        det = d.get("detection", {})
        cv = d.get("cross_venue", {})
        kel = d.get("kelly", {})
        risk = d.get("risk", {})
        lat = d.get("latency", {})

        def _num(value: Any, field_name: str) -> float:
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise DataValidationError(
                    f"strategy.yaml: {field_name} must be numeric, got {value!r}"
                ) from exc

        return cls(
            detection=DetectionSettings(
                min_net_edge=_num(det.get("min_net_edge", 0.005), "detection.min_net_edge"),
                min_liquidity_contracts=_num(
                    det.get("min_liquidity_contracts", 50), "detection.min_liquidity_contracts"
                ),
                max_book_age_seconds=_num(
                    det.get("max_book_age_seconds", 5), "detection.max_book_age_seconds"
                ),
            ),
            cross_venue=CrossVenueSettings(
                min_match_confidence=_num(
                    cv.get("min_match_confidence", 0.85), "cross_venue.min_match_confidence"
                ),
                min_net_edge=_num(cv.get("min_net_edge", 0.01), "cross_venue.min_net_edge"),
            ),
            kelly=KellySettings(
                fraction=_num(kel.get("fraction", 0.5), "kelly.fraction"),
                assumed_edge_probability=_num(
                    kel.get("assumed_edge_probability", 0.55), "kelly.assumed_edge_probability"
                ),
                arbitrage_p_win=_num(kel.get("arbitrage_p_win", 0.99), "kelly.arbitrage_p_win"),
            ),
            risk=RiskSettings(
                max_position_per_market=_num(
                    risk.get("max_position_per_market", 500), "risk.max_position_per_market"
                ),
                max_portfolio_exposure=_num(
                    risk.get("max_portfolio_exposure", 5000), "risk.max_portfolio_exposure"
                ),
                max_venue_exposure=_num(
                    risk.get("max_venue_exposure", 3000), "risk.max_venue_exposure"
                ),
                max_daily_loss=_num(risk.get("max_daily_loss", 100.0), "risk.max_daily_loss"),
                max_acceptable_latency_ms=_num(
                    risk.get("max_acceptable_latency_ms", 2000), "risk.max_acceptable_latency_ms"
                ),
            ),
            latency=LatencySettings(
                data_ms=_num(
                    lat.get("assumed_data_latency_ms", 250), "latency.assumed_data_latency_ms"
                ),
                processing_ms=_num(
                    lat.get("assumed_processing_latency_ms", 50),
                    "latency.assumed_processing_latency_ms",
                ),
                execution_ms=_num(
                    lat.get("assumed_execution_latency_ms", 500),
                    "latency.assumed_execution_latency_ms",
                ),
                adverse_drift_per_second=_num(
                    lat.get("adverse_drift_per_second", 0.002), "latency.adverse_drift_per_second"
                ),
                drift_is_placeholder=bool(lat.get("drift_is_placeholder", True)),
            ),
        )

    @classmethod
    def load(cls) -> StrategyConfig:
        """Load ``configs/strategy.yaml`` into a StrategyConfig.

        Raises:
            ConfigurationError: if ``strategy.yaml`` is missing.
            DataValidationError: if a threshold value is not numeric.
        """
        return cls.from_dict(load_yaml("strategy.yaml"))

    @property
    def total_latency_seconds(self) -> float:
        """Sum of data + processing + execution latency, in seconds.

        Raises:
            None.
        """
        return (
            self.latency.data_ms + self.latency.processing_ms + self.latency.execution_ms
        ) / 1000.0
