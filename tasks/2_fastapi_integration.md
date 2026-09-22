# Task 2: FastAPI integration — streaming + approval + caching

## Purpose
Wrap `src/agent/agent.py` in a FastAPI service with server-sent events, two-step approval, schema caching, and configurable LLM providers.

## Requirements
- `POST /api/query` with `stream: true/false` and `approve: true/false`
- `POST /api/approve` to execute a draft action from `/query`
- `GET /api/schema` and `POST /api/refresh-schema` (cached introspection)
- `GET /api/health`
- Per-request `provider` / `model_name` override
- Schema singleton with `AgentSingleton` cache
- Streaming via `_stream_agent_run()` yielding SSE events
- `requirements.txt` and `pyproject.toml` updated for `fastapi`, `uvicorn`, `sse-starlette`

## Scope
- New code: `src/api/` (router, schemas, deps, main)
- Modified: `src/agent/agent.py` (added `_provider`, `_model_name`, `_rebuild_llm()`)
- Not changed: SQL builder, validation, DB access logic
- Tests out of scope

## Notes
- Agent's `run()` is synchronous; streaming is coarse-grained (post-phase events)
- Use `uv sync` / `uv run uvicorn src.api.main:app` to start
