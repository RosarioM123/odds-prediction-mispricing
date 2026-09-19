"""Paper execution engine: simulated fills against order-book constraints.
No real orders are ever placed (Phase 3: backend/execution/paper.py)."""

from backend.execution.paper import PaperBroker, latest_book_at

__all__ = ["PaperBroker", "latest_book_at"]
