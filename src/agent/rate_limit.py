"""Shared LLM call pacing + retry-on-429 helper.

Both BaselineAgent and Agent call an LLM once per eval case. Under load
(e.g. the eval harness running 11 cases back to back), Groq's rate limit
kicks in — and once one call gets rate-limited, langchain_groq's own
internal retry (max_retries=2) burns more of the rate-limit window on its
own sleep-and-retry, which can cascade into repeated 429s on the *next*
case too.

Two independent mitigations, used together:

  1. `throttle()` — a process-wide minimum spacing between outgoing LLM
     calls, enforced *before* we ever make the request. This is the
     cheap, boring fix: never approach the limit in the first place,
     rather than reacting after the fact.

  2. `invoke_with_backoff()` — a wrapper around `.invoke(...)` that catches
     429s (and a specific Groq quirk described below) and retries with
     exponential backoff + jitter, honoring the API's Retry-After header
     when present.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Minimum seconds between any two outgoing LLM calls made by this process,
# across both BaselineAgent and Agent (they share this module-level state).
# Override via env var if you're on a plan with a higher/lower rate limit.
_MIN_INTERVAL_SECONDS = float(os.getenv("LLM_MIN_REQUEST_INTERVAL_SECONDS", "3.0"))

_lock = threading.Lock()
_last_call_monotonic = 0.0


def throttle() -> None:
    """Block until at least _MIN_INTERVAL_SECONDS have passed since the
    last LLM call made anywhere in this process."""
    global _last_call_monotonic
    with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SECONDS - (now - _last_call_monotonic)
        if wait > 0:
            time.sleep(wait)
        _last_call_monotonic = time.monotonic()


def _is_retryable(exc: Exception) -> bool:
    """True for 429s and the Groq tool-routing quirk described above."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    if "429" in text or "rate limit" in text or "rate_limit" in text:
        return True
    if "tool_use_failed" in text or ("tool 'json'" in text and "not in request.tools" in text):
        return True
    return False


def _retry_after_seconds(exc: Exception) -> float | None:
    resp = getattr(exc, "response", None)
    header = getattr(resp, "headers", None)
    if not header:
        return None
    value = header.get("retry-after") or header.get("Retry-After")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def invoke_with_backoff(
    invoke_fn: Callable[..., T],
    *args: Any,
    max_attempts: int = 5,
    base_delay: float = 5.0,
    **kwargs: Any,
) -> T:
    """Call invoke_fn(*args, **kwargs), retrying on 429 / transient Groq
    tool-routing errors with exponential backoff + jitter. Always throttles
    before each attempt so retries don't just re-trigger the same limit."""
    attempt = 0
    while True:
        throttle()
        try:
            return invoke_fn(*args, **kwargs)
        except Exception as exc:
            attempt += 1
            if not _is_retryable(exc) or attempt >= max_attempts:
                raise
            delay = _retry_after_seconds(exc)
            if delay is None:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            logger.warning(
                f"LLM call rate-limited/transient error (attempt {attempt}/{max_attempts}); "
                f"sleeping {delay:.1f}s before retry. {exc}"
            )
            time.sleep(delay)
