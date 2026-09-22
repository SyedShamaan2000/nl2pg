# Task 3: Fix QueryResponse validation error for `limit` field

## Purpose

The `/query` endpoint throws a 500 error (pydantic `ValidationError`) when the agent returns `limit` as an integer (e.g., `"give me 10 recent customers"`). The API schema expected `limit` as `list[int]` while the agent internal model declared it as `int` with an invalid `default_factory=list`. This mismatch caused `QueryResponse(**result)` to fail.

## Requirements

- Change `src/models/schemas.py`: `ProposedAction.limit` from `int = Field(default_factory=list)` to `int | None = None`
- Change `src/api/schemas.py`: `ProposedActionAPI.limit` from `list[int] = Field(default_factory=list)` to `int | None = None`
- Update `src/agent/agent.py`: wire `limit` into `build_sql` for SELECT queries (`LIMIT n`)

## Scope

- In: `src/models/schemas.py`, `src/api/schemas.py`, `src/agent/agent.py`
- Out: new tests, LLM prompt changes (existing structured output already emits `limit` correctly)

## Notes

- The LLM emits `limit` as a single integer for requests like "10 recent customers"; making both models consistent resolves the validation crash.
- SQL builder now respects `limit` so the query actually returns N rows.
