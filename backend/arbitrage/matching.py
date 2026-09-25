"""Cross-venue event matching: which markets are the same event?

Two-stage design:

1. ``match_markets`` scores every cross-venue pair with a confidence in
   [0, 1]: token-set similarity of the normalized titles (weight 0.9)
   plus an expiry-proximity bonus (0.1 when both expirations fall within
   24 hours). Exact title equality still scores 0.9 before the bonus,
   exactly as the old deterministic matcher did.
2. The detection call sites (``detect_cross_venue_direct`` /
   ``detect_cross_venue_complement``) apply the ``min_match_confidence``
   gate (default 0.85). The matcher only *proposes*; the gate
   *authorizes*. A fuzzy title match can never reach the risk engine
   below the gate.

A manual override list (``configs/match_overrides.yaml``) pins known
pairs: curators can force-match equivalents the scorer misses or
exclude false friends it over-scores. Overrides are version-controlled
and carry a reason string each.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from backend.config import CONFIG_DIR
from backend.errors import DataValidationError
from backend.schemas import Market

SAME_VENUE = "SAME_VENUE_NOT_CROSS"

#: Token-similarity floor. Pairs below this are not candidates at all;
#: the min_match_confidence gate (default 0.85) does the real filtering.
MIN_JACCARD_CANDIDATE = 0.5


@dataclass
class MatchResult:
    matched: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)


def normalize_question(question: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Raises:
        None.
    """
    text = question.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity over whitespace tokens of normalized text.

    Raises:
        None.
    """
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _market_ref(venue: str, market_id: str) -> str:
    return f"{venue}:{market_id}"


def override_key(a: Market, b: Market) -> str:
    """Order-independent key for a cross-venue market pair.

    Raises:
        None.
    """
    refs = sorted(
        [_market_ref(a.venue.value, a.market_id), _market_ref(b.venue.value, b.market_id)]
    )
    return "|".join(refs)


def load_overrides(path: str | Path | None = None) -> dict[str, bool]:
    """Load the manual match-override list.

    Returns a mapping of override keys to True (force-match) or False
    (force-exclude). Missing file means no overrides, which is the
    normal state.

    Raises:
        DataValidationError: if the file exists but is malformed.
    """
    p = Path(path) if path else CONFIG_DIR / "match_overrides.yaml"
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise DataValidationError(f"override file {p}: invalid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("overrides"), list):
        raise DataValidationError(f"override file {p}: expected top-level 'overrides' list")
    overrides: dict[str, bool] = {}
    for entry in raw["overrides"]:
        if not isinstance(entry, dict):
            raise DataValidationError(f"override file {p}: each entry must be a mapping")
        markets = entry.get("markets")
        equivalent = entry.get("equivalent")
        if (
            not isinstance(markets, list)
            or len(markets) != 2
            or not all(isinstance(m, str) for m in markets)
            or not isinstance(equivalent, bool)
        ):
            raise DataValidationError(
                f"override file {p}: entry needs 'markets: [venue:id, venue:id]' "
                f"and 'equivalent: true|false'"
            )
        key = "|".join(sorted(markets))
        overrides[key] = equivalent
    return overrides


def match_markets(a: Market, b: Market, *, overrides: dict[str, bool] | None = None) -> MatchResult:
    """Score whether two markets describe the same event.

    Confidence = 0.9 * title token-Jaccard + 0.1 expiry bonus (both
    expirations known and within 24h), capped at 1.0. Pairs below the
    Jaccard candidate floor are rejected outright; everything else is a
    scored candidate for the min_match_confidence gate downstream.

    Raises:
        None.
    """
    if a.venue == b.venue:
        return MatchResult(False, 0.0, [SAME_VENUE])
    if overrides:
        forced = overrides.get(override_key(a, b))
        if forced is True:
            return MatchResult(True, 1.0, ["manual override: curator-confirmed equivalent"])
        if forced is False:
            return MatchResult(False, 0.0, ["manual override: curator-excluded"])
    qa, qb = normalize_question(a.question), normalize_question(b.question)
    if not qa or not qb:
        return MatchResult(False, 0.0, ["empty question after normalization"])
    jaccard = token_jaccard(qa, qb)
    if jaccard < MIN_JACCARD_CANDIDATE:
        return MatchResult(
            False, round(jaccard, 3), [f"title similarity {jaccard:.2f} below candidate floor"]
        )
    confidence = 0.9 * jaccard
    reasons = [f"title token similarity {jaccard:.2f}"]
    if jaccard == 1.0:
        reasons.append("normalized questions exactly equal")
    if a.expiration and b.expiration:
        delta = abs((a.expiration - b.expiration).total_seconds())
        if delta <= 24 * 3600:
            confidence = min(1.0, confidence + 0.1)
            reasons.append("expirations within 24h")
        else:
            reasons.append("expirations differ by >24h; no bonus")
    return MatchResult(True, round(confidence, 3), reasons)


def default_overrides() -> dict[str, bool]:
    """Load overrides from the conventional config location.

    Raises:
        DataValidationError: if the file exists but is malformed.
    """
    return load_overrides()
