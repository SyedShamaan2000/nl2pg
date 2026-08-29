"""Pydantic models for agent outputs and schema definitions."""

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class Filter(BaseModel):
    """A single WHERE clause filter."""
    column: str
    operator: Literal["=", "!=", ">", "<", ">=", "<=", "IN", "LIKE"] = "="
    value: Any


class ProposedAction(BaseModel):
    """The agent's proposed database action (boundary object)."""
    action: Literal["select", "insert", "update", "delete"]
    table: str
    filters: list[Filter] = Field(default_factory=list)
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
