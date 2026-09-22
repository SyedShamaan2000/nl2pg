"""API request/response schemas (separate from agent's internal schemas)."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    """Incoming request to /query endpoint."""

    request: str = Field(..., description="Natural language request")
    provider: Literal["groq", "gemini"] | None = Field(
        None, description="Override LLM provider for this request"
    )
    model_name: str | None = Field(None, description="Override model name for this request")
    stream: bool = Field(
        False,
        description="If true, return results as Server-Sent Events (SSE).",
    )
    approve: bool = Field(
        False,
        description="If true and action is a write, execute immediately (skip two-step).",
    )


class ApproveRequest(BaseModel):
    """Request to /approve endpoint — the action returned by /query."""

    action: "ProposedActionAPI"  # forward ref
    # We accept the full action object back; server re-validates it.


class SchemaResponse(BaseModel):
    """Response for GET /schema."""

    tables: list["TableInfoAPI"] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Response for GET /health."""

    status: Literal["ok"] = "ok"
    version: str = "1.0.0"


class RefreshSchemaResponse(BaseModel):
    """Response for POST /refresh-schema."""

    refreshed: bool
    tables_count: int


# --- Streaming event types ---

StreamEventType = Literal[
    "start", "reasoning", "action", "sql", "execution", "rows", "error", "done"
]


class StreamEvent(BaseModel):
    """Single SSE event."""

    type: StreamEventType
    data: Any = None
    # For errors, data is a string message


# --- Mirror of agent schemas but serializable for API ---


class FilterAPI(BaseModel):
    column: str
    operator: Literal["=", "!=", ">", "<", ">=", "<=", "IN", "LIKE"] = "="
    value: Any


class JoinAPI(BaseModel):
    table: str
    on_left: str
    on_right: str
    join_type: Literal["INNER", "LEFT"] = "INNER"


class ProposedActionAPI(BaseModel):
    action: Literal["select", "insert", "update", "delete", "clarify"]
    table: str = ""
    joins: list[JoinAPI] = Field(default_factory=list)
    filters: list[FilterAPI] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    having: list[FilterAPI] = Field(default_factory=list)
    limit: int | None = None
    values: dict[str, Any] | None = None
    reasoning: str


class ColumnInfoAPI(BaseModel):
    name: str
    type: str
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_table: str | None = None
    foreign_column: str | None = None
    has_default: bool = False
    is_unique: bool = False


class TableInfoAPI(BaseModel):
    name: str
    columns: list[ColumnInfoAPI]


class QueryResponse(BaseModel):
    """Response for /query (non-streaming) or final result."""

    raw_action: ProposedActionAPI | None = None
    sql: str | None = None
    executed: bool = False
    approval_required: bool = False
    approved: bool | None = None
    rows: list[dict[str, Any]] | None = None
    error: str | None = None
    clarification: str | None = None


# Update forward refs
ApproveRequest.model_rebuild()
SchemaResponse.model_rebuild()
