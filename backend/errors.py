"""Structured exception taxonomy for the ODDS research engine.

Every error raised by ``backend/`` derives from :class:`OddsEngineError`,
so callers can catch the whole family with a single handler. Classes that
replace a builtin (``ValueError``, ``FileNotFoundError``) subclass that
builtin as well, so existing ``pytest.raises(ValueError)`` contracts and
``except FileNotFoundError`` handlers keep working unchanged.

Design notes
------------
* This module imports nothing from ``backend``: it is the bottom of the
  dependency graph, so every other backend module can import it safely.
* Errors are raised for *caller* or *environment* faults (bad arguments,
  malformed venue payloads, missing config, venue outages). Expected
  runtime states -- empty books, shortfalls, risk rejections -- are
  returned as data (``None``, ``WalkResult.shortfall``, ``GateResult``),
  never raised.
"""

from __future__ import annotations


class OddsEngineError(Exception):
    """Root of the ODDS engine exception hierarchy.

    Catch this to handle any engine-raised error without listing each
    subclass.
    """


class DataValidationError(OddsEngineError, ValueError):
    """A value violated a domain invariant.

    Raised for bad caller arguments (invalid order-book side, unknown
    snapshot label, out-of-range Kelly probability, invalid Kelly
    fraction, invalid adapter env) and for malformed persisted data
    (unsupported snapshot version, non-mapping config file). Subclasses
    ``ValueError`` so ``pytest.raises(ValueError)`` assertions keep
    passing.
    """


class AdapterParseError(DataValidationError):
    """A venue API payload had an unexpected shape.

    Raised when a venue returns a structurally unrecognized document --
    e.g. Polymarket ``/book`` returning a non-dict, or a Kalshi order-book
    envelope that matches neither the classic cents nor the ``fp``
    dollars layout. This is a venue-contract fault, not a caller fault,
    but it stays a ``ValueError`` subclass so snapshot/adapter tests are
    unaffected.
    """


class ConfigurationError(OddsEngineError, FileNotFoundError):
    """Engine configuration is missing or unusable.

    Raised when a required ``configs/*.yaml`` file does not exist.
    Subclasses ``FileNotFoundError`` so existing file-missing handlers
    keep working.
    """


class InsufficientLiquidityError(OddsEngineError):
    """An operation required a minimum fill depth that was not available.

    Reserved for paths that must guarantee a fill size (e.g. a future
    live-execution path). The paper engine represents this state as data
    instead: ``walk_book`` reports ``WalkResult.shortfall`` and detectors
    return ``None`` below ``min_liquidity_contracts``.
    """


class ExecutionRejectedError(OddsEngineError):
    """An order or execution request was rejected.

    Reserved for a future live-execution path where a venue or broker can
    reject an order. The paper engine never places real orders; rejections
    there are modeled as ``PaperTradeStatus.MISSED`` / ``EXPIRED``.
    """


class VenueError(OddsEngineError):
    """Venue communication failure.

    Raised for HTTP errors, timeouts, transport failures, and non-JSON
    responses from a venue's public endpoints. No credentials are ever
    involved: every endpoint used by the adapters is public.
    """


class RateLimitError(VenueError):
    """The venue throttled us (HTTP 429); back off before retrying."""
