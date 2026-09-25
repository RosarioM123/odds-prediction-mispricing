"""SQLite store for timestamped version-1 market snapshots.

The historical dataset for backtesting lives here: every poll of the
live venues appends one row per venue per round, keeping the
``odds_snapshot_version: 1`` JSON payload intact. ``iter_inputs``
returns ``SnapshotInput`` rows in chronological order, ready for
``ReplayEngine``.

The file itself is runtime data (gitignored); the schema and the
import/export helpers are the versioned part.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from backend.backtesting.replay import SnapshotInput
from backend.errors import DataValidationError
from backend.schemas import Market, OrderBook, Venue

SNAPSHOT_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    label TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_captured ON snapshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_venue ON snapshots(venue);
"""


def _validate(payload: dict[str, Any]) -> None:
    if payload.get("odds_snapshot_version") != SNAPSHOT_VERSION:
        raise DataValidationError("unsupported snapshot version")
    for key in ("venue", "label", "captured_at", "markets", "order_books"):
        if key not in payload:
            raise DataValidationError(f"snapshot missing {key!r}")


class SnapshotStore:
    """Append-only SQLite store of version-1 snapshots."""

    def __init__(self, path: str | Path) -> None:
        """Open (creating) the store at ``path``.

        Raises:
            None.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)

    def append(self, payload: dict[str, Any]) -> int:
        """Append one version-1 snapshot payload. Returns the row id.

        Raises:
            DataValidationError: if the payload is not a version-1
                snapshot.
        """
        _validate(payload)
        cur = self._conn.execute(
            "INSERT INTO snapshots (venue, label, captured_at, payload) VALUES (?, ?, ?, ?)",
            (
                str(payload["venue"]),
                str(payload["label"]),
                str(payload["captured_at"]),
                json.dumps(payload),
            ),
        )
        self._conn.commit()
        row_id = cur.lastrowid
        if row_id is None:  # unreachable: the INSERT just succeeded
            raise DataValidationError("snapshot insert returned no row id")
        return row_id

    def import_directory(self, directory: str | Path) -> int:
        """Import every ``*.json`` version-1 snapshot in a directory.

        Files that fail validation are skipped with a printed warning,
        never half-imported.

        Raises:
            None.
        """
        count = 0
        for path in sorted(Path(directory).glob("*.json")):
            try:
                payload = json.loads(path.read_text())
                self.append(payload)
                count += 1
            except (DataValidationError, ValueError, OSError) as exc:
                print(f"skip {path.name}: {exc}")
        return count

    def iter_inputs(
        self,
        *,
        venue: str | None = None,
        label: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[SnapshotInput]:
        """Load snapshots as ``SnapshotInput`` in chronological order.

        Raises:
            DataValidationError: if a stored payload fails to parse
                (should be impossible: payloads are validated on append).
        """
        query = "SELECT payload FROM snapshots WHERE 1=1"
        params: list[str] = []
        if venue:
            query += " AND venue = ?"
            params.append(venue)
        if label:
            query += " AND label = ?"
            params.append(label)
        if since:
            query += " AND captured_at >= ?"
            params.append(since)
        if until:
            query += " AND captured_at <= ?"
            params.append(until)
        query += " ORDER BY captured_at ASC"
        inputs: list[SnapshotInput] = []
        for (payload_json,) in self._conn.execute(query, params):
            payload = json.loads(payload_json)
            v = Venue(payload["venue"])
            inputs.append(
                SnapshotInput(
                    label=payload["label"],
                    venue=v,
                    markets=[Market(**m) for m in payload["markets"]],
                    books=[OrderBook(**b) for b in payload["order_books"]],
                    captured_at=datetime.fromisoformat(payload["captured_at"]),
                )
            )
        return inputs

    def stats(self) -> dict[str, Any]:
        """Row counts and time span. Raises: None."""
        row = self._conn.execute(
            "SELECT COUNT(*), MIN(captured_at), MAX(captured_at) FROM snapshots"
        ).fetchone()
        by_venue = dict(
            self._conn.execute("SELECT venue, COUNT(*) FROM snapshots GROUP BY venue").fetchall()
        )
        return {
            "n_snapshots": row[0],
            "first": row[1],
            "last": row[2],
            "by_venue": by_venue,
            "path": str(self.path),
        }

    def close(self) -> None:
        """Close the connection. Raises: None."""
        self._conn.close()
