"""FastAPI router for NL-to-SQL agent API.

Provides:
- POST /api/query      : ingest natural-language request, return draft or stream.
- POST /api/approve    : execute a draft action from /query (for writes).
- GET  /api/schema     : introspected database schema (cached).
- POST /api/refresh-schema : bust cache, re-introspect.
- GET  /api/health     : liveness probe.

Streaming mode (SSE):
- Triggered via {"stream": true} in the request body.
- Yields JSON-encoded events over Server-Sent Events.
- Event types: start, reasoning, action, sql, execution, rows, error, done.
"""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse

from src.agent.agent import Agent
from src.api.deps import AgentSingleton, get_agent, run_sync
from src.api.schemas import (
    ApproveRequest,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    RefreshSchemaResponse,
    SchemaResponse,
    TableInfoAPI,
)
from src.db.introspect import introspect_schema
from src.models.schemas import Filter, Join, ProposedAction

logger = logging.getLogger(__name__)

router = APIRouter()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager: warm the AgentSingleton on startup."""
    logger.info("Starting up NL-to-SQL agent API")
    AgentSingleton.get()
    yield
    logger.info("Shutting down NL-to-SQL agent API")


app = FastAPI(
    title="NL-to-SQL Agent API",
    description=(
        "API for natural-language to SQL query execution with schema "
        "validation, two-step approval, and optional streaming."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(router)


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def _sse_format(data: Any) -> str:
    """Format a payload as a single SSE 'data:' line."""
    if not isinstance(data, (dict, list, str, int, float, bool)) and data is not None:
        data = str(data)
    return f"data: {json.dumps(data, default=str)}\n\n"


async def _stream_agent_run(
    agent: Agent, request: str
) -> AsyncGenerator[str, None]:
    """Run the agent end-to-end and yield SSE-formatted events."""
    yield _sse_format({"type": "start", "data": "Processing request"})

    def _run() -> dict[str, Any]:
        return agent.run(request)

    result = await run_sync(_run)

    if result.get("clarification"):
        yield _sse_format({"type": "reasoning", "data": result["clarification"]})
        yield _sse_format({"type": "done", "data": "Clarification requested"})
        return

    if result.get("raw_action") is not None:
        yield _sse_format({"type": "action", "data": result["raw_action"]})

    if result.get("sql"):
        yield _sse_format({"type": "sql", "data": result["sql"]})

    if result.get("executed"):
        yield _sse_format({"type": "execution", "data": "Execution completed"})

    if result.get("rows"):
        yield _sse_format({"type": "rows", "data": result["rows"]})

    if result.get("error"):
        yield _sse_format({"type": "error", "data": result["error"]})

    yield _sse_format({"type": "done", "data": "Processing completed"})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe."""
    return HealthResponse(status="ok", version="1.0.0")


@app.get("/schema", response_model=SchemaResponse)
async def get_schema() -> SchemaResponse:
    """Return the cached introspected schema."""
    tables = get_agent().tables
    schema_response = SchemaResponse(
        tables=[
            TableInfoAPI(
                name=t.name,
                columns=[
                    {
                        "name": col.name,
                        "type": col.type,
                        "nullable": col.nullable,
                        "is_primary_key": col.is_primary_key,
                        "is_foreign_key": col.is_foreign_key,
                        "foreign_table": col.foreign_table,
                        "foreign_column": col.foreign_column,
                        "has_default": col.has_default,
                        "is_unique": col.is_unique,
                    }
                    for col in t.columns
                ],
            )
            for t in tables
        ]
    )
    return schema_response


@app.post("/refresh-schema", response_model=RefreshSchemaResponse)
async def refresh_schema() -> RefreshSchemaResponse:
    """Bust the schema cache and re-introspect the live database."""
    AgentSingleton.refresh()
    tables = get_agent().tables
    return RefreshSchemaResponse(refreshed=True, tables_count=len(tables))


def _build_internal_action(api_action) -> ProposedAction:
    """Translate the API action into the internal Pydantic model."""
    return ProposedAction(
        action=api_action.action,
        table=api_action.table,
        joins=[Join(**j.model_dump()) for j in api_action.joins],
        filters=[Filter(**f.model_dump()) for f in api_action.filters],
        group_by=api_action.group_by,
        having=[Filter(**f.model_dump()) for f in api_action.having],
        values=api_action.values,
        reasoning=api_action.reasoning,
    )


@app.post("/query")
async def query(
    request: QueryRequest,
    req: Request,  # noqa: ARG001
) -> Response:
    """Process a natural-language request.

    Modes:
    - **Streaming** (`stream: true`): returns Server-Sent Events.
    - **Non-streaming** (default): returns a single JSON response.
      Writes return `approval_required: true` and a `raw_action`;
      the client POSTs that to /api/approve to execute.
    - **Auto-approve** (`approve: true`): executes a write in one
      round-trip. Convenience for trusted callers; do not expose publicly.
    """
    agent_singleton = get_agent()
    inner: Agent = agent_singleton.agent

    original_provider = inner._provider
    original_model = inner._model_name

    if request.provider is not None:
        inner._provider = request.provider
        inner.llm = inner._rebuild_llm()
    if request.model_name is not None:
        inner._model_name = request.model_name
        inner.llm = inner._rebuild_llm()

    try:
        if request.stream:
            async def event_gen() -> AsyncGenerator[str, None]:
                async for chunk in _stream_agent_run(inner, request.request):
                    yield chunk

            return StreamingResponse(
                event_gen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        # Non-streaming
        result = inner.run(request.request)

        # Auto-approve: execute a write in one round-trip.
        if request.approve and result.get("approval_required") and not result.get("error"):
            action = result.get("raw_action")
            if action is not None:
                from src.api.schemas import ProposedActionAPI
                api_action = ProposedActionAPI(**action)
                internal = _build_internal_action(api_action)
                tables = agent_singleton.tables
                validation_error = inner._validate_action(
                    internal, tables, request.request
                )
                if validation_error:
                    return JSONResponse(
                        status_code=400,
                        content=QueryResponse(
                            raw_action=action,
                            sql=None,
                            executed=False,
                            approval_required=True,
                            approved=False,
                            rows=None,
                            error=f"Validation error: {validation_error}",
                            clarification=None,
                        ).model_dump(),
                    )
                sql, error, rows = await run_sync(inner._execute_action, internal)
                return JSONResponse(
                    content=QueryResponse(
                        raw_action=action,
                        sql=sql,
                        executed=error is None,
                        approval_required=False,
                        approved=True,
                        rows=rows,
                        error=error,
                        clarification=None,
                    ).model_dump()
                )

        return JSONResponse(content=QueryResponse(**result).model_dump())
    finally:
        if request.provider is not None:
            inner._provider = original_provider
        if request.model_name is not None:
            inner._model_name = original_model
        if request.provider is not None or request.model_name is not None:
            inner.llm = inner._rebuild_llm()


@app.post("/approve")
async def approve(
    request: ApproveRequest,
    req: Request,  # noqa: ARG001
) -> Response:
    """Execute a draft action previously returned by /query.

    Re-validates against the current schema before execution.
    Returns the execution result with `executed: true` and `approved: true`.
    """
    agent_singleton = get_agent()
    inner: Agent = agent_singleton.agent
    internal = _build_internal_action(request.action)

    # Re-validate against the current schema.
    tables = agent_singleton.tables
    validation_error = inner._validate_action(internal, tables, request.action.reasoning)
    if validation_error:
        return JSONResponse(
            status_code=400,
            content=QueryResponse(
                raw_action=request.action.model_dump(),
                sql=None,
                executed=False,
                approval_required=True,
                approved=False,
                rows=None,
                error=f"Validation error: {validation_error}",
                clarification=None,
            ).model_dump(),
        )

    sql, error, rows = await run_sync(inner._execute_action, internal)

    return JSONResponse(
        content=QueryResponse(
            raw_action=request.action.model_dump(),
            sql=sql,
            executed=error is None,
            approval_required=False,
            approved=True,
            rows=rows,
            error=error,
            clarification=None,
        ).model_dump()
    )


__all__ = ["app"]