"""Fuzz the Kalshi/Polymarket adapter parse functions with malformed payloads.

Contract under test: for ANY input, each parse function must either return a
parsed result (``Market`` / ``OrderBook`` / list, possibly empty) or raise a
regular ``Exception``. Nothing that is not an ``Exception`` subclass
(``SystemExit``, ``KeyboardInterrupt``, ...) may escape, and every case must
terminate.

Specific exception types are deliberately NOT asserted: an error taxonomy is
being built concurrently.

Out of scope: the network-touching methods (``fetch_*``,
``resolve_taker_fee_rate``) -- this file only fuzzes pure parsing.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.markets.kalshi import KalshiAdapter
from backend.markets.polymarket import PolymarketAdapter
from backend.schemas import Market, OrderBook, Outcome, Venue
from tests.fixtures import make_market

_KA = KalshiAdapter()
_PMA = PolymarketAdapter()

_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=24),
    st.binary(max_size=16),
)
_garbage = st.recursive(
    _scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(max_size=12), children, max_size=5),
        st.tuples(children, children),
    ),
    max_leaves=6,
)
# Non-dict payloads exercise the ``raw.get`` / ``raw[...]`` paths; dict-shaped
# payloads exercise wrong types, missing keys, NaN/inf, nested garbage, and
# empty containers.
_payload = st.one_of(
    _garbage,
    st.dictionaries(st.text(max_size=16), _garbage, max_size=8),
)


def _check_total(fn, *args, expected_type):
    """Pass iff ``fn`` returns ``expected_type`` or raises a regular Exception.

    Anything escaping that is not an ``Exception`` subclass
    (``SystemExit``, ``KeyboardInterrupt``, ...) fails the test.
    """
    try:
        result = fn(*args)
    except Exception:
        return  # regular exceptions are acceptable; taxonomy TBD
    except BaseException as exc:  # noqa: BLE001 -- the point of this test
        pytest.fail(
            f"{fn.__qualname__} leaked {type(exc).__name__} (not an Exception subclass): {exc!r}"
        )
    assert isinstance(result, expected_type), (
        f"{fn.__qualname__} returned {type(result).__name__}, expected {expected_type.__name__}"
    )


@given(payload=_payload)
@settings(max_examples=75, deadline=None)
def test_kalshi_normalize_market_never_escapes(payload):
    _check_total(_KA.normalize_market, payload, expected_type=Market)


@given(payload=_payload, outcome=st.sampled_from([Outcome.YES, Outcome.NO]))
@settings(max_examples=75, deadline=None)
def test_kalshi_normalize_order_book_never_escapes(payload, outcome):
    market = make_market(venue=Venue.KALSHI, outcome=outcome, taker_fee_rate=None)
    _check_total(_KA.normalize_order_book, payload, market, expected_type=OrderBook)


@given(payload=_payload)
@settings(max_examples=75, deadline=None)
def test_kalshi_markets_from_kalshi_never_escapes(payload):
    _check_total(_KA._markets_from_kalshi, payload, expected_type=list)


@given(payload=_payload)
@settings(max_examples=75, deadline=None)
def test_polymarket_normalize_market_never_escapes(payload):
    _check_total(_PMA.normalize_market, payload, expected_type=Market)


@given(payload=_payload, outcome=st.sampled_from([Outcome.YES, Outcome.NO]))
@settings(max_examples=75, deadline=None)
def test_polymarket_normalize_order_book_never_escapes(payload, outcome):
    market = make_market(venue=Venue.POLYMARKET, outcome=outcome)
    _check_total(_PMA.normalize_order_book, payload, market, expected_type=OrderBook)


@given(payload=_payload)
@settings(max_examples=75, deadline=None)
def test_polymarket_markets_from_gamma_never_escapes(payload):
    _check_total(_PMA._markets_from_gamma, payload, expected_type=list)
