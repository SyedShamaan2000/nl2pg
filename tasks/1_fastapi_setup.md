# Task 1: Setup FastAPI project for NL-to-SQL agent

## Purpose
Convert the existing schema-aware NL-to-SQL agent (agent.py) into a FastAPI application with streaming support and two-step approval for writes.

## Requirements
- Create FastAPI app with endpoints:
  - POST /query: accepts natural language request, returns draft action (if approval needed) or streams results.
  - POST /approve: executes a previously drafted action (for writes).
  - GET /schema: returns introspected database schema.
  - GET /health: health check.
  - POST /refresh-schema: bust schema cache.
- Support streaming (Server-Sent Events) for reasoning, SQL, and results.
- Two-step approval: /query returns approval_required: true + draft action; /approve executes it.
- LLM provider/model configurable via startup env and overridable per request.
- Cache schema introspection until explicitly refreshed.
- Keep existing CLI functionality for backward compatibility.
- Add requirements.txt (using uv) and Dockerfile/docker-compose.yml.
- Update README with setup and usage instructions.

## Scope
- Modify src/agent/agent.py minimally (if at all) – prefer wrapping or extending.
- New code under src/api/.
- Do not change agent's core logic (validation, SQL building, etc.) unless necessary for API integration.
- Tests out of scope for this task (but note existing eval cases).

## Notes
- Use pydantic-settings for configuration.
- Use async where beneficial (streaming, DB calls via asyncpg? but note current agent uses psycopg2 – we may keep sync and run in threads for streaming, or switch to asyncpg. However, to minimize risk, we'll keep psycopg2 and use run_in_threadpool for DB calls in async endpoints.
- Streaming: use EventSourceResponse (from sse-starlette) or manual StreamingResponse with media_type="text/event-stream".
- Approval endpoint must validate the action comes from a recent draft (could store in memory or require signing; for simplicity, we'll accept the action and re-validate – but note: we must ensure it's the same as the one returned by /query. We'll rely on re-validation and assume no state tampering in this scope).