"""Human-approval gate for write actions.

Ground Rule #4: no write executes without explicit approval.
This module provides the CLI approval function; callers inject it
via Agent(approval_fn=...).
"""

import logging
from src.models.schemas import ProposedAction

logger = logging.getLogger(__name__)


def cli_approval(action: ProposedAction) -> bool:
    """Prompt user for approval; return True only if 'y'."""
    print(f"\n[APPROVAL REQUIRED] {action.action.upper()} on {action.table}")
    print(f"  reasoning: {action.reasoning}")
    print(f"  values: {action.values}")
    print(f"  filters: {action.filters}")
    resp = input("Approve? [y/N]: ").strip().lower()
    approved = resp == "y"
    logger.info(f"User approval for {action.action}: {approved}")
    return approved
