"""Market snapshot save/load for demo and historical modes.

Snapshots let the engine run without live API access: capture order books
once, then replay detection, backtesting, and paper execution offline.
Every snapshot carries a label so simulated data is never mistaken for
live data:

  label "live"      captured from a venue API at `captured_at`
  label "simulated" hand-built or generated fixtures (tests, demos)

Files live under data/raw/ (gitignored, regenerable).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.errors import DataValidationError
from backend.schemas import Market, OrderBook, Venue

LABEL_LIVE = "live"
LABEL_SIMULATED = "simulated"


def save_snapshot(
    path: str | Path,
    venue: Venue,
    markets: list[Market],
    books: list[OrderBook],
    *,
    label: str = LABEL_LIVE,
) -> Path:
    """Persist markets and order books to a labeled JSON snapshot.

    Raises:
        DataValidationError: if ``label`` is not "live" or "simulated".
    """
    if label not in (LABEL_LIVE, LABEL_SIMULATED):
        raise DataValidationError(f"label must be '{LABEL_LIVE}' or '{LABEL_SIMULATED}'")
    payload: dict[str, Any] = {
        "odds_snapshot_version": 1,
        "label": label,
        "venue": venue.value,
        "captured_at": datetime.now(UTC).isoformat(),
        "markets": [m.model_dump(mode="json") for m in markets],
        "order_books": [b.model_dump(mode="json") for b in books],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return path


def load_snapshot(path: str | Path) -> tuple[str, Venue, list[Market], list[OrderBook]]:
    """Load a snapshot; returns (label, venue, markets, books).

    Raises:
        DataValidationError: if the snapshot version is unsupported.
    """
    payload: dict[str, Any] = json.loads(Path(path).read_text())
    if payload.get("odds_snapshot_version") != 1:
        raise DataValidationError("unsupported snapshot version")
    venue = Venue(payload["venue"])
    markets = [Market(**m) for m in payload["markets"]]
    books = [OrderBook(**b) for b in payload["order_books"]]
    return payload["label"], venue, markets, books
