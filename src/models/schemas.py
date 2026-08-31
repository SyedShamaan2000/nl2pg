"""Pydantic models for agent outputs and schema definitions."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentOutputParseError(Exception):
    """Raised when LLM output cannot be parsed/validated into a Pydantic model."""


class Filter(BaseModel):
    """A single WHERE / HAVING clause filter.

    `column` is normally a real column name (optionally qualified as
    "table.column" when the query involves a JOIN). For HAVING clauses it
    may instead be an aggregate expression such as "COUNT(orders.id)" —
    callers should not run column-existence checks against expressions
    that contain parentheses.
    """

    column: str
    operator: Literal["=", "!=", ">", "<", ">=", "<=", "IN", "LIKE"] = "="
    value: Any


class Join(BaseModel):
    """A single JOIN clause.

    on_left / on_right are fully qualified "table.column" references, e.g.
    on_left="orders.customer_id", on_right="customers.id". One side must
    refer to the main `table` on ProposedAction, or to a table introduced
    by an earlier join in the list, so joins always chain back to
    something already in scope. (Checked at validation time in the agent,
    not enforced by this model.)
    """

    table: str
    on_left: str
    on_right: str
    join_type: Literal["INNER", "LEFT"] = "INNER"


class ProposedAction(BaseModel):
    """The agent's proposed database action (boundary object).

    Supports SELECT (with optional JOIN / GROUP BY / HAVING), INSERT,
    UPDATE, DELETE. The agent may also return action="clarify" when the
    intent is ambiguous, in which case `reasoning` carries the
    clarification question.

    `joins` is only meaningful for action="select". Writes never join —
    UPDATE/DELETE ... FROM has different (easier to get wrong) semantics,
    and no current use case needs it, so it's kept out of scope
    deliberately rather than half-supported.
    """

    action: Literal["select", "insert", "update", "delete", "clarify"]
    table: str = ""
    joins: list[Join] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    having: list[Filter] = Field(default_factory=list)
    limit: int = Field(default_factory=list)
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
    # True when the column has a DB-side DEFAULT (SERIAL/IDENTITY, a UUID
    # generator, now()-style timestamp defaults, etc). The agent uses this
    # to avoid inventing values (e.g. a placeholder id) for columns the
    # database will populate itself.
    has_default: bool = False
    # True when the column has a UNIQUE constraint (single-column or part of
    # a composite unique index). The agent uses this to pre-check INSERT
    # values for uniqueness violations before execution.
    is_unique: bool = False


class TableInfo(BaseModel):
    """Schema table metadata."""

    name: str
    columns: list[ColumnInfo]
