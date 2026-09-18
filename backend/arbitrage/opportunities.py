"""Mispricing detection on normalized order books.

Strategies
----------
1. Bundle arbitrage (same event): buy the YES ask and the NO ask. A
   YES+NO pair always settles to exactly $1, so
       raw_edge = 1 - (yes_ask + no_ask)
   is a risk-free edge at the touch when positive.
2. Cross-venue arbitrage (deterministic match first):
   a. Direct: same outcome, buy on venue A's ask, sell on venue B's bid
      when bid_B > ask_A. Requires real bids on the sell side; a venue
      whose public book has no bids (Kalshi) is skipped, never invented.
   b. Complement: buy YES on venue A and NO on venue B when
      ask_YES_A + ask_NO_B < 1. Works without any bids.

Matching is deterministic: normalized questions must be exactly equal
(plus an expiry-proximity bonus). A future NLP matcher may *propose*
candidates with confidence scores, but it can never authorize a trade;
only pairs passing the deterministic gate (>= min_match_confidence)
reach the risk engine.

Every opportunity carries a human-readable explanation: venues, prices,
size, the full cost waterfall, and net edge, plus any data-quality flags
(STALE, FEE_RATE_FALLBACK, SPREAD_UNAVAILABLE_NO_BIDS, ...). Simulated
inputs stay labeled simulated all the way through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.arbitrage.costs import (
    FeeModel,
    build_cost_breakdown,
    half_spread_cost,
    latency_adjustment,
    walk_book,
)
from backend.arbitrage.settings import StrategyConfig
from backend.schemas import (
    CostBreakdown,
    Market,
    MarketStatus,
    Opportunity,
    OrderBook,
    Outcome,
    Venue,
    utcnow,
)

STALE_BOOK = "STALE_BOOK"
EMPTY_BOOK = "EMPTY_BOOK"
CONFLICTING_METADATA = "CONFLICTING_METADATA"
BELOW_MIN_EDGE = "BELOW_MIN_EDGE"
INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
NO_BIDS_FOR_DIRECT = "NO_BIDS_FOR_DIRECT_LEG"
MATCH_TOO_WEAK = "MATCH_TOO_WEAK"
SAME_VENUE = "SAME_VENUE_NOT_CROSS"


@dataclass
class BookView:
    """One market plus its order book plus the snapshot label."""

    market: Market
    book: OrderBook
    label: str  # "live" | "simulated"


def book_timestamp(view: BookView) -> datetime:
    return view.book.venue_timestamp or view.book.received_timestamp


def book_age_seconds(view: BookView, now: datetime) -> float:
    return (now - book_timestamp(view)).total_seconds()


def is_stale(view: BookView, now: datetime, max_age_s: float) -> bool:
    return book_age_seconds(view, now) > max_age_s


def dedupe_views(views: list[BookView]) -> list[BookView]:
    """Drop duplicate (venue, market_id) pairs, keeping the newest quote."""
    best: dict[tuple[str, str], BookView] = {}
    for v in views:
        key = (v.market.venue.value, v.market.market_id)
        if key not in best or book_timestamp(v) > book_timestamp(best[key]):
            best[key] = v
    return list(best.values())


def normalize_question(question: str) -> str:
    text = question.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class MatchResult:
    matched: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)


def deterministic_match(a: Market, b: Market) -> MatchResult:
    """Score whether two markets are the same event, deterministically."""
    if a.venue == b.venue:
        return MatchResult(False, 0.0, [SAME_VENUE])
    qa, qb = normalize_question(a.question), normalize_question(b.question)
    if not qa or not qb or qa != qb:
        return MatchResult(False, 0.0, ["questions differ after normalization"])
    confidence = 0.9
    reasons = ["normalized questions exactly equal"]
    if a.expiration and b.expiration:
        delta = abs((a.expiration - b.expiration).total_seconds())
        if delta <= 24 * 3600:
            confidence = min(1.0, confidence + 0.1)
            reasons.append("expirations within 24h")
        else:
            reasons.append("expirations differ by >24h; no bonus")
    return MatchResult(True, round(confidence, 3), reasons)


def _explain(summary: str, fields: dict) -> dict:
    return {"summary": summary, **fields}


def _finalize(*, strategy: str, legs: list[dict], size: float,
              raw_edge_pc: float, fees_total: float, slippage_total: float,
              spread_info_pc: float, latency_total: float,
              liquidity: float, match: MatchResult | None,
              flags: list[str], detail: dict) -> Opportunity:
    costs = build_cost_breakdown(
        raw_edge_per_contract=raw_edge_pc, size=size,
        fees_total=fees_total, slippage_total=slippage_total,
        spread_info_per_contract=spread_info_pc, latency_total=latency_total)
    opp_id = (f"{strategy}-{detail.get('venue_tag', 'x')}-"
              f"{utcnow().strftime('%Y%m%d%H%M%S%f')}")
    return Opportunity(
        opportunity_id=opp_id,
        strategy=strategy,
        legs=legs,
        costs=costs,
        liquidity=round(liquidity, 6),
        match_confidence=match.confidence if match else None,
        explanation=_explain(detail.pop("summary"), {
            **detail,
            "raw_edge_per_contract": round(raw_edge_pc, 6),
            "fees_per_contract": costs.trading_fees,
            "slippage_per_contract": costs.slippage,
            "spread_info_per_contract": costs.spread_cost,
            "latency_per_contract": costs.latency_adjustment,
            "net_edge_per_contract": costs.net_edge,
            "size_contracts": round(size, 6),
            "flags": sorted(set(flags)),
            "match_reasons": match.reasons if match else [],
        }),
    )


def detect_bundle_arbitrage(yes_view: BookView, no_view: BookView, *,
                            config: StrategyConfig, fee_model: FeeModel,
                            now: datetime | None = None,
                            size_cap: float | None = None) -> Opportunity | None:
    """Same-event YES/NO bundle arbitrage. Returns None when not executable."""
    now = now or utcnow()
    det = config.detection
    flags: list[str] = [f"LABEL_{yes_view.label.upper()}"]
    if yes_view.market.event_id != no_view.market.event_id:
        return None
    for v in (yes_view, no_view):
        if is_stale(v, now, det.max_book_age_seconds):
            flags.append(STALE_BOOK)
            return None
        if not v.book.asks:
            flags.append(EMPTY_BOOK)
            return None
    yes_ask = yes_view.book.best_ask
    no_ask = no_view.book.best_ask
    raw_edge = 1.0 - (yes_ask + no_ask)
    if raw_edge <= 0:
        return None
    depth = min(yes_view.book.ask_depth, no_view.book.ask_depth)
    size = min(depth, size_cap) if size_cap else depth
    if size < det.min_liquidity_contracts:
        return None

    walk_y = walk_book(yes_view.book, "buy", size)
    walk_n = walk_book(no_view.book, "buy", size)
    slippage_total = ((walk_y.vwap - yes_ask) + (walk_n.vwap - no_ask)) * size
    fee_y = fee_model.taker_fee(yes_view.market, size, walk_y.vwap)
    fee_n = fee_model.taker_fee(no_view.market, size, walk_n.vwap)
    fees_total = fee_y.fee_dollars + fee_n.fee_dollars
    flags.extend(fee_y.notes + fee_n.notes)
    lat_total, lat_notes = latency_adjustment(
        config.total_latency_seconds,
        config.latency.adverse_drift_per_second,
        config.latency.drift_is_placeholder)
    latency_total = lat_total * size
    flags.extend(lat_notes)
    spread_y, sy_notes = half_spread_cost(yes_view.book)
    spread_n, sn_notes = half_spread_cost(no_view.book)
    flags.extend(sy_notes + sn_notes)
    spread_info = (spread_y or 0.0) + (spread_n or 0.0)

    costs_probe = build_cost_breakdown(
        raw_edge_per_contract=raw_edge, size=size, fees_total=fees_total,
        slippage_total=slippage_total, spread_info_per_contract=spread_info,
        latency_total=latency_total)
    if costs_probe.net_edge < det.min_net_edge:
        return None

    venue = yes_view.market.venue.value
    legs = [
        {"venue": venue, "market_id": yes_view.market.market_id,
         "outcome": Outcome.YES.value, "side": "buy",
         "quantity": round(size, 6), "expected_price": yes_ask},
        {"venue": no_view.market.venue.value, "market_id": no_view.market.market_id,
         "outcome": Outcome.NO.value, "side": "buy",
         "quantity": round(size, 6), "expected_price": no_ask},
    ]
    summary = (
        f"Bundle arbitrage on {venue}: buy {size:.0f} YES @ ${yes_ask:.3f} and "
        f"{size:.0f} NO @ ${no_ask:.3f} (YES+NO settles to $1.00). Raw edge "
        f"${raw_edge:.4f}/contract; fees ${costs_probe.trading_fees:.4f}, "
        f"slippage ${costs_probe.slippage:.4f}, latency "
        f"${costs_probe.latency_adjustment:.4f} -> net "
        f"${costs_probe.net_edge:.4f}/contract.")
    return _finalize(
        strategy="bundle_arbitrage", legs=legs, size=size,
        raw_edge_pc=raw_edge, fees_total=fees_total,
        slippage_total=slippage_total, spread_info_pc=spread_info,
        latency_total=latency_total, liquidity=depth, match=None,
        flags=flags,
        detail={"summary": summary, "venue_tag": venue,
                "event_id": yes_view.market.event_id,
                "yes_ask": yes_ask, "no_ask": no_ask,
                "vwap_yes": walk_y.vwap, "vwap_no": walk_n.vwap})


def detect_cross_venue_direct(buy_view: BookView, sell_view: BookView, *,
                              config: StrategyConfig, fee_model: FeeModel,
                              now: datetime | None = None,
                              size_cap: float | None = None) -> Opportunity | None:
    """Same outcome, buy on venue A ask, sell on venue B bid.

    Skipped (never invented) when the sell venue publishes no bids.
    """
    now = now or utcnow()
    cv = config.cross_venue
    det = config.detection
    match = deterministic_match(buy_view.market, sell_view.market)
    if not match.matched or match.confidence < cv.min_match_confidence:
        return None
    flags: list[str] = [f"LABEL_{buy_view.label.upper()}"]
    for v in (buy_view, sell_view):
        if is_stale(v, now, det.max_book_age_seconds):
            return None
        if not v.book.asks:
            return None
    if not sell_view.book.bids:
        return None  # NO_BIDS_FOR_DIRECT: refuse to invent a sell price
    ask = buy_view.book.best_ask
    bid = sell_view.book.best_bid
    raw_edge = bid - ask
    if raw_edge <= 0:
        return None
    depth = min(buy_view.book.ask_depth, sell_view.book.bid_depth)
    size = min(depth, size_cap) if size_cap else depth
    if size < det.min_liquidity_contracts:
        return None

    walk_buy = walk_book(buy_view.book, "buy", size)
    walk_sell = walk_book(sell_view.book, "sell", size)
    slippage_total = ((walk_buy.vwap - ask) + (bid - walk_sell.vwap)) * size
    fee_b = fee_model.taker_fee(buy_view.market, size, walk_buy.vwap)
    fee_s = fee_model.taker_fee(sell_view.market, size, walk_sell.vwap)
    fees_total = fee_b.fee_dollars + fee_s.fee_dollars
    flags.extend(fee_b.notes + fee_s.notes)
    lat_total, lat_notes = latency_adjustment(
        config.total_latency_seconds,
        config.latency.adverse_drift_per_second,
        config.latency.drift_is_placeholder)
    flags.extend(lat_notes)
    spread_b, _ = half_spread_cost(buy_view.book)
    spread_s, _ = half_spread_cost(sell_view.book)
    spread_info = (spread_b or 0.0) + (spread_s or 0.0)

    costs_probe = build_cost_breakdown(
        raw_edge_per_contract=raw_edge, size=size, fees_total=fees_total,
        slippage_total=slippage_total, spread_info_per_contract=spread_info,
        latency_total=lat_total * size)
    if costs_probe.net_edge < cv.min_net_edge:
        return None

    legs = [
        {"venue": buy_view.market.venue.value,
         "market_id": buy_view.market.market_id,
         "outcome": buy_view.market.outcome.value, "side": "buy",
         "quantity": round(size, 6), "expected_price": ask},
        {"venue": sell_view.market.venue.value,
         "market_id": sell_view.market.market_id,
         "outcome": sell_view.market.outcome.value, "side": "sell",
         "quantity": round(size, 6), "expected_price": bid},
    ]
    summary = (
        f"Cross-venue arbitrage ({buy_view.market.venue.value} -> "
        f"{sell_view.market.venue.value}, match confidence "
        f"{match.confidence:.2f}): buy {size:.0f} "
        f"{buy_view.market.outcome.value} @ ${ask:.3f}, sell @ ${bid:.3f}. "
        f"Raw edge ${raw_edge:.4f}/contract -> net "
        f"${costs_probe.net_edge:.4f}/contract after costs.")
    return _finalize(
        strategy="cross_venue_arbitrage", legs=legs, size=size,
        raw_edge_pc=raw_edge, fees_total=fees_total,
        slippage_total=slippage_total, spread_info_pc=spread_info,
        latency_total=lat_total * size, liquidity=depth, match=match,
        flags=flags,
        detail={"summary": summary,
                "venue_tag": f"{buy_view.market.venue.value}-{sell_view.market.venue.value}",
                "buy_ask": ask, "sell_bid": bid,
                "match_confidence": match.confidence})


def detect_cross_venue_complement(yes_view: BookView, no_view: BookView, *,
                                  config: StrategyConfig, fee_model: FeeModel,
                                  now: datetime | None = None,
                                  size_cap: float | None = None) -> Opportunity | None:
    """Buy YES on venue A and NO on venue B for the same event.

    Needs only asks, so it works even when a venue publishes no bids.
    """
    now = now or utcnow()
    cv = config.cross_venue
    det = config.detection
    match = deterministic_match(yes_view.market, no_view.market)
    if not match.matched or match.confidence < cv.min_match_confidence:
        return None
    flags: list[str] = [f"LABEL_{yes_view.label.upper()}"]
    for v in (yes_view, no_view):
        if is_stale(v, now, det.max_book_age_seconds):
            return None
        if not v.book.asks:
            return None
    yes_ask = yes_view.book.best_ask
    no_ask = no_view.book.best_ask
    raw_edge = 1.0 - (yes_ask + no_ask)
    if raw_edge <= 0:
        return None
    depth = min(yes_view.book.ask_depth, no_view.book.ask_depth)
    size = min(depth, size_cap) if size_cap else depth
    if size < det.min_liquidity_contracts:
        return None

    walk_y = walk_book(yes_view.book, "buy", size)
    walk_n = walk_book(no_view.book, "buy", size)
    slippage_total = ((walk_y.vwap - yes_ask) + (walk_n.vwap - no_ask)) * size
    fee_y = fee_model.taker_fee(yes_view.market, size, walk_y.vwap)
    fee_n = fee_model.taker_fee(no_view.market, size, walk_n.vwap)
    fees_total = fee_y.fee_dollars + fee_n.fee_dollars
    flags.extend(fee_y.notes + fee_n.notes)
    lat_total, lat_notes = latency_adjustment(
        config.total_latency_seconds,
        config.latency.adverse_drift_per_second,
        config.latency.drift_is_placeholder)
    flags.extend(lat_notes)
    spread_info = 0.0
    for v in (yes_view, no_view):
        s, s_notes = half_spread_cost(v.book)
        spread_info += s or 0.0
        flags.extend(s_notes)

    costs_probe = build_cost_breakdown(
        raw_edge_per_contract=raw_edge, size=size, fees_total=fees_total,
        slippage_total=slippage_total, spread_info_per_contract=spread_info,
        latency_total=lat_total * size)
    if costs_probe.net_edge < cv.min_net_edge:
        return None

    legs = [
        {"venue": yes_view.market.venue.value,
         "market_id": yes_view.market.market_id,
         "outcome": Outcome.YES.value, "side": "buy",
         "quantity": round(size, 6), "expected_price": yes_ask},
        {"venue": no_view.market.venue.value,
         "market_id": no_view.market.market_id,
         "outcome": Outcome.NO.value, "side": "buy",
         "quantity": round(size, 6), "expected_price": no_ask},
    ]
    summary = (
        f"Cross-venue complement arbitrage ({yes_view.market.venue.value} YES "
        f"+ {no_view.market.venue.value} NO, match confidence "
        f"{match.confidence:.2f}): buy {size:.0f} YES @ ${yes_ask:.3f} and "
        f"{size:.0f} NO @ ${no_ask:.3f}; pair settles to $1.00. Raw edge "
        f"${raw_edge:.4f}/contract -> net "
        f"${costs_probe.net_edge:.4f}/contract after costs.")
    return _finalize(
        strategy="cross_venue_arbitrage", legs=legs, size=size,
        raw_edge_pc=raw_edge, fees_total=fees_total,
        slippage_total=slippage_total, spread_info_pc=spread_info,
        latency_total=lat_total * size, liquidity=depth, match=match,
        flags=flags,
        detail={"summary": summary,
                "venue_tag": f"{yes_view.market.venue.value}-{no_view.market.venue.value}",
                "yes_ask": yes_ask, "no_ask": no_ask,
                "match_confidence": match.confidence})


def detect_all(views: list[BookView], *, config: StrategyConfig,
               fee_model: FeeModel, now: datetime | None = None,
               size_cap: float | None = None) -> tuple[list[Opportunity], list[str]]:
    """Run every detector over a set of views.

    Handles duplicates (keeps newest), skips events with conflicting
    metadata, and returns (opportunities, global_flags).
    """
    now = now or utcnow()
    views = dedupe_views(views)
    global_flags: list[str] = []

    # Group by event; conflicting questions within an event -> skip group.
    by_event: dict[str, list[BookView]] = {}
    for v in views:
        by_event.setdefault(v.market.event_id, []).append(v)
    usable: list[BookView] = []
    for event_id, group in by_event.items():
        questions = {normalize_question(v.market.question) for v in group}
        if len(questions) > 1:
            global_flags.append(f"{CONFLICTING_METADATA}:{event_id}")
            continue
        usable.extend(group)

    opportunities: list[Opportunity] = []
    # 1. Bundle arb within each (venue, event).
    by_venue_event: dict[tuple[str, str], dict[str, BookView]] = {}
    for v in usable:
        key = (v.market.venue.value, v.market.event_id)
        by_venue_event.setdefault(key, {})[v.market.outcome.value] = v
    for (venue, _event), outcomes in by_venue_event.items():
        if Outcome.YES.value in outcomes and Outcome.NO.value in outcomes:
            opp = detect_bundle_arbitrage(
                outcomes[Outcome.YES.value], outcomes[Outcome.NO.value],
                config=config, fee_model=fee_model, now=now, size_cap=size_cap)
            if opp:
                opportunities.append(opp)

    # 2. Cross-venue pairs.
    for i, va in enumerate(usable):
        for vb in usable[i + 1:]:
            if va.market.venue == vb.market.venue:
                continue
            if va.market.outcome == vb.market.outcome:
                opp = detect_cross_venue_direct(
                    va, vb, config=config, fee_model=fee_model,
                    now=now, size_cap=size_cap)
                if opp:
                    opportunities.append(opp)
                # also try the reverse direction
                opp = detect_cross_venue_direct(
                    vb, va, config=config, fee_model=fee_model,
                    now=now, size_cap=size_cap)
                if opp:
                    opportunities.append(opp)
            else:
                yes_v, no_v = (va, vb) if va.market.outcome == Outcome.YES else (vb, va)
                opp = detect_cross_venue_complement(
                    yes_v, no_v, config=config, fee_model=fee_model,
                    now=now, size_cap=size_cap)
                if opp:
                    opportunities.append(opp)

    return opportunities, sorted(set(global_flags))
