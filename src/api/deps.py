"""Shared dependencies: singleton Agent, schema cache, FastAPI dependency injection."""

from __future__ import annotations

import logging
from typing import Any, Callable, Generator

from fastapi import Request

from src.agent.agent import Agent, ProposedAction
from src.db.introspect import introspect_schema

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Singleton Agent with schema cache
# ---------------------------------------------------------------------------

class AgentSingleton:
    """Module-level cached agent instance.

    Holds the introspected schema and the LLM-backed Agent. The schema
    is refreshed only when `refresh()` is called (e.g. via
    POST /refresh-schema) or the process restarts.
    """

    _instance: AgentSingleton | None = None

    def __init__(self, provider: str | None = None, model_name: str | None = None):
        self.provider = provider
        self.model_name = model_name
        self.tables = introspect_schema()
        self.agent = Agent(
            provider=provider,
            model_name=model_name,
        )
        logger.info(
            f"AgentSingleton initialized with {len(self.tables)} tables "
            f"(provider={provider}, model={model_name})"
        )

    @classmethod
    def get(cls) -> AgentSingleton:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def refresh(cls) -> AgentSingleton:
        """Bust the cache and re-introspect the schema."""
        old_provider = cls._instance.provider if cls._instance else None
        old_model = cls._instance.model_name if cls._instance else None
        cls._instance = cls(provider=old_provider, model_name=old_model)
        return cls._instance


# ---------------------------------------------------------------------------
# FastAPI dependency helpers
# ---------------------------------------------------------------------------

def get_agent() -> AgentSingleton:
    """Dependency that returns the cached singleton AgentSingleton."""
    return AgentSingleton.get()


async def get_agent_async(request: Request) -> AgentSingleton:
    """Async-compatible dependency; returns the cached singleton."""
    return AgentSingleton.get()


# We store a reference to the current agent singleton in request state
# so /approve can access the same instance that generated the draft.
def get_agent_for_request(request: Request) -> AgentSingleton:
    return request.state.agent


# Helper to run a sync callable in a threadpool without blocking the event loop
def run_sync(sync_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run a blocking sync function in a threadpool."""
    import asyncio
    return asyncio.get_event_loop().run_in_executor(None, sync_fn, *args, **kwargs)
