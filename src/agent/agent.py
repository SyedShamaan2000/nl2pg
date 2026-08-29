"""Schema-aware, validated NL-to-SQL agent.

This is the real solution (Agent v1), as opposed to the naive baseline.
Key differences from baseline:
  - Receives introspected schema as context before generating a query.
  - Output is validated against a Pydantic ProposedAction schema.
  - Table/column names are checked against the live schema before execution.
  - All write actions (insert/update/delete) require human approval (see approval.py).
  - DEBUG-level logs at every decision point.

Ground Rule #4 is non-negotiable: no write action auto-executes.
"""

import logging
import os
from typing import Any

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI

from src.db.connection import get_connection
from src.db.introspect import (
    column_exists,
    get_table,
    introspect_schema,
    schema_to_text,
)
from src.models.schemas import Filter, ProposedAction

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL builder (action -> SQL string)
# ---------------------------------------------------------------------------


def build_sql(action: ProposedAction) -> str:
    table = action.table
    filters = action.filters

    def _condition(f: Filter) -> str:
        val = f.value
        if f.operator == "IN":
            if isinstance(val, (list, tuple)):
                items = ", ".join(repr(v) for v in val)
            else:
                items = repr(val)
            return f"{f.column} IN ({items})"
        if f.operator == "LIKE":
            return f"{f.column} LIKE {repr(val)}"
        return f"{f.column} {f.operator} {repr(val)}"

    where = ""
    if filters:
        clauses = " AND ".join(_condition(f) for f in filters)
        where = f" WHERE {clauses}"

    if action.action == "select":
        return f"SELECT * FROM {table}{where};"
    if action.action == "update":
        if not action.values:
            raise ValueError("UPDATE action requires non-None values")
        pairs = ", ".join(f"{k} = {repr(v)}" for k, v in action.values.items())
        return f"UPDATE {table} SET {pairs}{where};"
    if action.action == "delete":
        return f"DELETE FROM {table}{where};"
    raise ValueError(f"Unknown action: {action.action}")


def _build_insert_sql(table: str, values: dict[str, Any]) -> str:
    cols = ", ".join(values.keys())
    vals = ", ".join(repr(v) for v in values.values())
    return f"INSERT INTO {table} ({cols}) VALUES ({vals});"


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class SchemaValidationError(Exception):
    pass


class Agent:
    def __init__(self, approval_fn: Any = None) -> None:
        self._approval_fn = approval_fn
        self._tables: list | None = None
        api_key = os.getenv("GEMINI_API_KEY") or ""
        if not api_key:
            logger.warning("GEMINI_API_KEY not set; agent LLM calls will fail.")
        kwargs: dict[str, object] = {"model": "gemini-3.5-flash", "temperature": 0.3}
        if api_key:
            kwargs["google_api_key"] = api_key
        self.llm = ChatGoogleGenerativeAI(**kwargs)  # type: ignore[arg-type]

    def run(self, request: str) -> dict[str, Any]:
        logger.info(f"Agent received request: {request!r}")
        tables = self._get_schema()
        schema_text = schema_to_text(tables)
        logger.debug(
            f"Schema context sent to agent ({len(schema_text)} chars):\n{schema_text[:500]}..."
        )

        raw_action, parse_error = self._generate_action(request, schema_text)
        if parse_error:
            logger.error(f"Agent output parse error: {parse_error}")
            return self._result(
                raw_action=None, sql=None, executed=False,
                approval_required=False, approved=None,
                rows=None, error=parse_error,
            )

        logger.debug(f"Agent parsed ProposedAction: {raw_action!r}")

        validation_error = self._validate_action(raw_action, tables)
        if validation_error:
            logger.warning(f"Schema validation failed: {validation_error}")
            return self._result(
                raw_action=raw_action.model_dump(),
                sql=None, executed=False,
                approval_required=False, approved=None,
                rows=None, error=f"Schema validation error: {validation_error}",
            )

        is_write = raw_action.action in ("insert", "update", "delete")
        if is_write:
            approved = self._request_approval(raw_action)
            logger.info(
                f"Approval result for {raw_action.action} on {raw_action.table}: "
                f"{'approved' if approved else 'denied'}"
            )
            if not approved:
                return self._result(
                    raw_action=raw_action.model_dump(),
                    sql=None, executed=False,
                    approval_required=True, approved=False,
                    rows=None, error="Approval denied.",
                )

        sql, exec_error, rows_result = self._execute_action(raw_action)
        if exec_error:
            logger.error(f"Execution error: {exec_error}")
            return self._result(
                raw_action=raw_action.model_dump(),
                sql=sql, executed=False,
                approval_required=is_write, approved=is_write,
                rows=None, error=exec_error,
            )

        logger.info(
            f"Action executed successfully: {raw_action.action} on {raw_action.table}"
        )
        return self._result(
            raw_action=raw_action.model_dump(),
            sql=sql, executed=True,
            approval_required=is_write, approved=is_write,
            rows=rows_result,
            error=None,
        )

    def _get_schema(self) -> list:
        if self._tables is None:
            self._tables = introspect_schema()
            if not self._tables:
                logger.warning("Introspection returned zero tables")
        return self._tables

    def _generate_action(
        self, request: str, schema_text: str
    ) -> tuple[ProposedAction | None, str | None]:
        if not request or not request.strip():
            return None, "Empty natural-language request."

        system_prompt = (
            "You are a database query planner. Respond ONLY with valid JSON.\n"
            "Shape: {\"action\":\"select\"|\"insert\"|\"update\"|\"delete\","
            "\"table\":\"...\",\"filters\":[{\"column\":\"...\",\"operator\":\"=\",\"value\":\"...\"}],"
            "\"values\":{...},\"reasoning\":\"...\"}\n"
            f"Schema:\n{schema_text}\n"
            "Rules: action in list; filter operators =,!=,>,<,>=,<=,IN,LIKE; "
            "UPDATE/DELETE require >=1 filter; INSERT requires values; SELECT uses WHERE filters."
        )
        try:
            structured_llm = self.llm.with_structured_output(ProposedAction)
            response = structured_llm.invoke(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Request: {request}"},
                ]
            )
            logger.debug(f"LLM raw response: {response!r}")
            return response, None
        except Exception as exc:
            logger.error(f"LLM call failed: {exc}")
            return None, f"LLM error: {exc}"

    def _validate_action(self, action: ProposedAction, tables: list) -> str | None:
        table_info = get_table(tables, action.table)
        if table_info is None:
            return f"Table '{action.table}' not found. Available: {[t.name for t in tables]}"
        for f in action.filters:
            if not column_exists(table_info, f.column):
                return f"Column '{f.column}' not in '{action.table}'."
        if action.values:
            for col in action.values.keys():
                if not column_exists(table_info, col):
                    return f"Column '{col}' in values not in '{action.table}'."
        if action.action in ("update", "delete") and not action.filters:
            return f"Safety: {action.action.upper()} has no WHERE filters."
        return None

    def _request_approval(self, action: ProposedAction) -> bool:
        if self._approval_fn is None:
            logger.warning("No approval_fn — denying write")
            return False
        try:
            return bool(self._approval_fn(action))
        except Exception as exc:
            logger.error(f"Approval raised: {exc} — denying")
            return False

    def _execute_action(self, action: ProposedAction) -> tuple[str | None, str | None, list | None]:
        try:
            with get_connection() as conn, conn.cursor() as cur:
                if action.action == "select":
                    sql = build_sql(action)
                    logger.debug(f"Executing SELECT: {sql}")
                    cur.execute(sql)
                    if cur.description is None:
                        return sql, "No rows returned.", None
                    cols = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    result = [dict(zip(cols, r)) for r in rows]
                    logger.info(f"SELECT returned {len(result)} row(s)")
                    return sql, None, result

                elif action.action in ("insert", "update", "delete"):
                    if action.action == "insert":
                        if not action.values:
                            return None, "INSERT requires values", None
                        sql = _build_insert_sql(action.table, action.values)
                    else:
                        sql = build_sql(action)
                    logger.debug(f"Executing write: {sql}")
                    cur.execute(sql)
                    rows_affected = cur.rowcount
                    conn.commit()
                    if rows_affected == 0 and action.action in ("update", "delete"):
                        logger.warning(f"{action.action.upper()} matched 0 rows")
                    logger.info(f"Write executed: {rows_affected} row(s)")
                    return sql, None, [{"rows_affected": rows_affected}]
                else:
                    return None, f"Unknown action: {action.action}", None
        except Exception as exc:
            logger.error(f"DB execution failed: {exc}")
            return None, f"DB error: {exc}", None

    def _result(self, raw_action, sql, executed, approval_required, approved, rows, error):
        return {
            "raw_action": raw_action,
            "sql": sql,
            "executed": executed,
            "approval_required": approval_required,
            "approved": approved,
            "rows": rows,
            "error": error,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    def cli_approval(action: ProposedAction) -> bool:
        print(f"\n[APPROVAL REQUIRED] {action.action.upper()} on {action.table}")
        print(f"  reasoning: {action.reasoning}")
        print(f"  values: {action.values}")
        print(f"  filters: {action.filters}")
        resp = input("Approve? [y/N]: ").strip().lower()
        return resp == "y"

    agent = Agent(approval_fn=cli_approval)
    request = input("Enter request: ")
    result = agent.run(request)
    print("\n=== Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
