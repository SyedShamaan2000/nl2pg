"""Pydantic models for agent outputs and schema definitions."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentOutputParseError(Exception):
    """Raised when LLM output cannot be parsed/validated into a Pydantic model."""


class Filter(BaseModel):
    """A single WHERE / HAVING clause filter."""

    column: str
    operator: Literal["=", "!=", ">", "<", ">=", "<=", "IN", "LIKE"] = "="
    value: Any


class ProposedAction(BaseModel):
    """The agent's proposed database action (boundary object).

    Supports basic SELECT, INSERT, UPDATE, DELETE. For aggregation queries
    (e.g. "customers with more than 10 orders"), use group_by and having.
    The agent may also return action="clarify" when the intent is ambiguous,
    in which case `reasoning` carries the clarification question.
    """

    action: Literal["select", "insert", "update", "delete", "clarify"]
    table: str = ""
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    having: list[Filter] = Field(default_factory=list)
    values: dict[str, Any] | None = None
    reasoning: str


class ColumnInfo(BaseModel):
    """Schema column metadata."""

    name: str
    type: str
    nullable: bool = True
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_table: str | None = None
    foreign_column: str | None = None


class TableInfo(BaseModel):
    """Schema table metadata."""

    name: str
    columns: list[ColumnInfo]
