"""Tests for the scored cross-venue matcher (backend/arbitrage/matching.py).

Title pairs below are representative cross-venue phrasings modeled on
real titles observed from the live Polymarket and Kalshi endpoints on
2026-09-25 (e.g. Polymarket's "Will Gavin Newsom win the 2028
Democratic presidential nomination?"); they exercise the scorer, not
any venue's live catalog.
"""

from datetime import timedelta

import pytest

from backend.arbitrage.matching import (
    load_overrides,
    match_markets,
    normalize_question,
    override_key,
    token_jaccard,
)
from backend.errors import DataValidationError
from backend.schemas import Venue
from tests.fixtures import T0, make_market


def _pair(qa, qb, **kw):
    a = make_market(venue=Venue.POLYMARKET, market_id="pm-1", question=qa, **kw)
    b = make_market(venue=Venue.KALSHI, market_id="ka-1", question=qb, **kw)
    return a, b


def test_exact_title_match_scores_090():
    a, b = _pair(
        "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
        "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
    )
    r = match_markets(a, b)
    assert r.matched
    assert r.confidence == pytest.approx(0.9)
    assert any("exactly equal" in x for x in r.reasons)


def test_expiry_proximity_bonus_caps_at_one():
    a, b = _pair("Xi Jinping out before 2027?", "Xi Jinping out before 2027?")
    a = make_market(
        venue=Venue.POLYMARKET,
        market_id="pm-1",
        question="Xi Jinping out before 2027?",
        expiration=T0,
    )
    b = make_market(
        venue=Venue.KALSHI,
        market_id="ka-1",
        question="Xi Jinping out before 2027?",
        expiration=T0 + timedelta(hours=2),
    )
    r = match_markets(a, b)
    assert r.matched
    assert r.confidence == pytest.approx(1.0)
    assert any("within 24h" in x for x in r.reasons)


def test_fuzzy_pair_is_candidate_below_default_gate():
    # Same event, different venue phrasing: proposes, does not authorize.
    a, b = _pair(
        "Will the Federal Reserve cut interest rates in September 2026?",
        "Will the Fed cut interest rates in September 2026?",
    )
    r = match_markets(a, b)
    assert r.matched  # candidate
    assert 0.5 <= r.confidence < 0.85  # below the default min_match_confidence


def test_different_nominees_rejected_by_gate_math():
    a, b = _pair(
        "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
        "Will Kamala Harris win the 2028 Democratic presidential nomination?",
    )
    r = match_markets(a, b)
    assert r.confidence < 0.85


def test_dissimilar_titles_not_candidates():
    a, b = _pair(
        "Will Gavin Newsom win the 2028 Democratic presidential nomination?",
        "Over 2.5 goals scored in the Champions League final?",
    )
    r = match_markets(a, b)
    assert not r.matched
    assert r.confidence < 0.5


def test_same_venue_never_matches():
    a = make_market(venue=Venue.POLYMARKET, market_id="pm-1", question="Same?")
    b = make_market(venue=Venue.POLYMARKET, market_id="pm-2", question="Same?")
    r = match_markets(a, b)
    assert not r.matched


def test_empty_question_rejected():
    a, b = _pair("!!!", "???")
    assert not match_markets(a, b).matched


def test_override_force_match(tmp_path):
    a, b = _pair("Totally different title one", "Totally different title two")
    key = override_key(a, b)
    cfg = tmp_path / "overrides.yaml"
    cfg.write_text(
        "overrides:\n"
        f"  - markets: [{key.split('|')[0]!r}, {key.split('|')[1]!r}]\n"
        "    equivalent: true\n"
        "    reason: curator verified same event\n"
    )
    ov = load_overrides(cfg)
    r = match_markets(a, b, overrides=ov)
    assert r.matched and r.confidence == 1.0
    assert any("override" in x for x in r.reasons)


def test_override_force_exclude(tmp_path):
    a, b = _pair("Identical title here?", "Identical title here?")
    key = override_key(a, b)
    cfg = tmp_path / "overrides.yaml"
    cfg.write_text(
        "overrides:\n"
        f"  - markets: [{key.split('|')[0]!r}, {key.split('|')[1]!r}]\n"
        "    equivalent: false\n"
        "    reason: same wording, different events\n"
    )
    ov = load_overrides(cfg)
    r = match_markets(a, b, overrides=ov)
    assert not r.matched


def test_override_key_is_order_independent():
    a = make_market(venue=Venue.POLYMARKET, market_id="pm-1", question="Q?")
    b = make_market(venue=Venue.KALSHI, market_id="ka-1", question="Q?")
    assert override_key(a, b) == override_key(b, a)


def test_load_overrides_missing_file_is_empty(tmp_path):
    assert load_overrides(tmp_path / "nope.yaml") == {}


def test_load_overrides_malformed_raises(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("overrides:\n  - markets: [only-one]\n    equivalent: true\n")
    with pytest.raises(DataValidationError):
        load_overrides(cfg)


def test_token_jaccard_basics():
    assert token_jaccard("a b c", "a b c") == pytest.approx(1.0)
    assert token_jaccard("a b", "c d") == pytest.approx(0.0)
    assert token_jaccard("", "a") == 0.0
    # Punctuation-insensitive via normalization upstream.
    assert normalize_question("Fed  cut?!  ") == "fed cut"
