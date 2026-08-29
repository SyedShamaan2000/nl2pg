"""Baseline: naive single-prompt approach (intentionally simple)."""

import logging
from src.db.connection import get_connection

logger = logging.getLogger(__name__)


class BaselineAgent:
    """Simple LLM agent with minimal context — deliberately naive."""

    def run(self, request: str) -> dict:
        """Take a natural-language request and return raw SQL.

        No schema context is injected. No validation. No approval gate.
        This is the comparison point for the real agent.
        """
        logger.debug(f"Baseline received request: {request!r}")
        # Simulate a naive LLM response: just echo a SQL guess
        # In a real baseline, this would call an LLM API
        raw_sql = self._guess_sql(request)
        logger.debug(f"Baseline generated SQL: {raw_sql!r}")
        return {
            "raw_sql": raw_sql,
            "request": request,
            "validated": False,
            "approval_required": False,
        }

    def _guess_sql(self, request: str) -> str:
        """Generate a naive SQL guess without schema awareness."""
        # Very naive heuristics — will likely be wrong
        request_lower = request.lower()
        if "customer" in request_lower:
            return "SELECT * FROM customers;"
        elif "order" in request_lower:
            return "SELECT * FROM orders;"
        elif "pending" in request_lower:
            return "SELECT * FROM orders WHERE status = 'pending';"
        else:
            return "SELECT * FROM orders;"
