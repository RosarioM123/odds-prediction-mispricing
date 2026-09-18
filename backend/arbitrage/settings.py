"""Typed strategy settings loaded from configs/strategy.yaml.

Every threshold the engine uses lives here, parsed once from the YAML so
detection, sizing, risk, and replay all share one source of truth. The raw
YAML stays the human-editable record; this module only types it.
"""
from __future__ import annotations

from dataclasses import dataclass

from backend.config import load_yaml


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
    def from_dict(cls, d: dict) -> "StrategyConfig":
        det = d.get("detection", {})
        cv = d.get("cross_venue", {})
        kel = d.get("kelly", {})
        risk = d.get("risk", {})
        lat = d.get("latency", {})
        return cls(
            detection=DetectionSettings(
                min_net_edge=float(det.get("min_net_edge", 0.005)),
                min_liquidity_contracts=float(det.get("min_liquidity_contracts", 50)),
                max_book_age_seconds=float(det.get("max_book_age_seconds", 5)),
            ),
            cross_venue=CrossVenueSettings(
                min_match_confidence=float(cv.get("min_match_confidence", 0.85)),
                min_net_edge=float(cv.get("min_net_edge", 0.01)),
            ),
            kelly=KellySettings(
                fraction=float(kel.get("fraction", 0.5)),
                assumed_edge_probability=float(kel.get("assumed_edge_probability", 0.55)),
                arbitrage_p_win=float(kel.get("arbitrage_p_win", 0.99)),
            ),
            risk=RiskSettings(
                max_position_per_market=float(risk.get("max_position_per_market", 500)),
                max_portfolio_exposure=float(risk.get("max_portfolio_exposure", 5000)),
                max_venue_exposure=float(risk.get("max_venue_exposure", 3000)),
                max_daily_loss=float(risk.get("max_daily_loss", 100.0)),
                max_acceptable_latency_ms=float(risk.get("max_acceptable_latency_ms", 2000)),
            ),
            latency=LatencySettings(
                data_ms=float(lat.get("assumed_data_latency_ms", 250)),
                processing_ms=float(lat.get("assumed_processing_latency_ms", 50)),
                execution_ms=float(lat.get("assumed_execution_latency_ms", 500)),
                adverse_drift_per_second=float(lat.get("adverse_drift_per_second", 0.002)),
                drift_is_placeholder=bool(lat.get("drift_is_placeholder", True)),
            ),
        )

    @classmethod
    def load(cls) -> "StrategyConfig":
        return cls.from_dict(load_yaml("strategy.yaml"))

    @property
    def total_latency_seconds(self) -> float:
        return (self.latency.data_ms + self.latency.processing_ms
                + self.latency.execution_ms) / 1000.0
