"""Arbitrage detectors: bundle arbitrage and cross-venue matching,
plus the transaction-cost model and Kelly sizing (Phase 3)."""

from backend.arbitrage.costs import FeeModel, walk_book
from backend.arbitrage.matching import (
    MatchResult,
    load_overrides,
    match_markets,
    normalize_question,
    token_jaccard,
)
from backend.arbitrage.opportunities import (
    BookView,
    detect_all,
    detect_bundle_arbitrage,
    detect_cross_venue_complement,
    detect_cross_venue_direct,
    deterministic_match,
)
from backend.arbitrage.settings import StrategyConfig
from backend.arbitrage.sizing import KELLY_FRACTIONS, size_position

__all__ = [
    "FeeModel",
    "walk_book",
    "BookView",
    "detect_all",
    "detect_bundle_arbitrage",
    "detect_cross_venue_complement",
    "detect_cross_venue_direct",
    "deterministic_match",
    "MatchResult",
    "load_overrides",
    "match_markets",
    "normalize_question",
    "token_jaccard",
    "StrategyConfig",
    "KELLY_FRACTIONS",
    "size_position",
]
