"""Schema-aware, validated NL-to-SQL agent (v2).

Iteration 2 changes vs v1:
  - Aggregation support: 'group_by' and 'having' fields on ProposedAction
    (Fixes: "customers with more than 10 orders" — no more naive SELECT *).
  - 'clarify' action: agent can ask for clarification instead of guessing.
  - System prompt now tells the LLM that Postgres supports GROUP BY, HAVING,
    JOIN, subqueries — explicitly with examples.
  - Semantic validation: catches obviously wrong queries (ambiguous table
    selection, etc.) before execution.
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


def _quote_literal(val: Any) -> str:
    """Format a Python value as a SQL literal."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    return repr(str(val))


def _render_filter(f: Filter) -> str:
    """Render a Filter into a SQL WHERE / HAVING expression."""
    val = f.value
    if f.operator == "IN":
        if isinstance(val, (list, tuple)):
            items = ", ".join(_quote_literal(v) for v in val)
        else:
            items = _quote_literal(val)
        return f"{f.column} IN ({items})"
    if f.operator == "LIKE":
        return f"{f.column} LIKE {_quote_literal(val)}"
    return f"{f.column} {f.operator} {_quote_literal(val)}"


def build_sql(action: ProposedAction) -> str:
    """Build SQL from a ProposedAction. Supports aggregation via group_by / having.

    SELECT with group_by: SELECT <group_by cols> FROM <table> [WHERE ...]
                          GROUP BY <cols> [HAVING ...]
    SELECT without group_by: SELECT * FROM <table> [WHERE ...]
    UPDATE / DELETE take the WHERE clause from filters only.
    """
    table = action.table
    filters = action.filters
    group_by = action.group_by
    having = action.having

    where = ""
    if filters:
        clauses = " AND ".join(_render_filter(f) for f in filters)
        where = f" WHERE {clauses}"

    if action.action == "select":
        if group_by:
            group_clause = ", ".join(group_by)
            having_clause = ""
            if having:
                having_clause = (
                    " HAVING " + " AND ".join(_render_filter(f) for f in having)
                )
            return (
                f"SELECT {group_clause} FROM {table}{where} "
                f"GROUP BY {group_clause}{having_clause};"
            )
        return f"SELECT * FROM {table}{where};"
    if action.action == "update":
        if not action.values:
            raise ValueError("UPDATE action requires non-None values")
        pairs = ", ".join(f"{k} = {_quote_literal(v)}" for k, v in action.values.items())
        return f"UPDATE {table} SET {pairs}{where};"
    if action.action == "delete":
        return f"DELETE FROM {table}{where};"
    raise ValueError(f"Unknown action: {action.action}")


def _build_insert_sql(table: str, values: dict[str, Any]) -> str:
    cols = ", ".join(values.keys())
    vals = ", ".join(_quote_literal(v) for v in values.values())
    return f"INSERT INTO {table} ({cols}) VALUES ({vals});"


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class SchemaValidationError(Exception):
    pass


# Phrases that strongly imply aggregation, used by semantic sanity check.
_AGGREGATION_HINTS = (
    "more than",
    "less than",
    "at least",
    "at most",
    "greater than",
    "fewer than",
    "count of",
    "number of",
    "sum of",
    "total of",
    "average",
    "per customer",
    "per user",
    "by customer",
    "by user",
    "group by",
    "having",
)


def _looks_like_aggregation_request(request: str) -> bool:
    """Heuristic: does the NL request seem to need GROUP BY / aggregation?"""
    r = request.lower()
    return any(hint in r for hint in _AGGREGATION_HINTS)


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
            f"Schema context sent to agent ({len(schema_text)} chars):\n"
            f"{schema_text[:500]}..."
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

        # Clarification short-circuit: agent said it needs more info.
        if raw_action.action == "clarify":
            logger.info("Agent requested clarification (no query executed).")
            return self._result(
                raw_action=raw_action.model_dump(),
                sql=None, executed=False,
                approval_required=False, approved=None,
                rows=None,
                error=None,
                clarification=raw_action.reasoning,
            )

        validation_error = self._validate_action(raw_action, tables, request)
        if validation_error:
            logger.warning(f"Schema/semantic validation failed: {validation_error}")
            return self._result(
                raw_action=raw_action.model_dump(),
                sql=None, executed=False,
                approval_required=False, approved=None,
                rows=None, error=f"Validation error: {validation_error}",
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
            "You are a Postgres query planner. Output ONLY JSON matching the "
            "ProposedAction schema. "
            "Postgres supports GROUP BY, HAVING, JOIN, and subqueries — use them "
            "when the request asks for counts, totals, or comparisons across "
            "groups (e.g. 'customers with more than 10 orders'). "
            "For aggregation: set group_by to the grouping columns (e.g. "
            "['orders.customer_id']) and put comparison filters (e.g. "
            "COUNT(*) > 10) in the 'having' list with a column like "
            "'count_orders' or 'count(*)'.\n\n"
            "If the user's request is genuinely ambiguous or you cannot map it "
            "to a clear query, set action='clarify' and put your question in "
            "the 'reasoning' field; do NOT guess a destructive action.\n\n"
            "Shape:\n"
            '{"action":"select"|"insert"|"update"|"delete"|"clarify",'
            '"table":"...","filters":[{"column":"...","operator":"=","value":"..."}],'
            '"group_by":["col",...],'
            '"having":[{"column":"count_*","operator":">","value":10}],'
            '"values":{...},"reasoning":"..."}\n\n'
            f"Schema:\n{schema_text}\n\n"
            "Rules: action must be one of the listed literals; "
            "filter operators =, !=, >, <, >=, <=, IN, LIKE; "
            "UPDATE/DELETE require >=1 filter (safety); "
            "INSERT requires non-null values; "
            "For SELECT with aggregation, also fill group_by / having."
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

    def _validate_action(
        self, action: ProposedAction, tables: list, request: str
    ) -> str | None:
        """Validate action against the live schema + a semantic sanity check."""
        if not action.table:
            return "Action must specify a table."

        table_info = get_table(tables, action.table)
        if table_info is None:
            return (
                f"Table '{action.table}' not found. "
                f"Available: {[t.name for t in tables]}"
            )
        for f in action.filters:
            if not column_exists(table_info, f.column):
                return f"Column '{f.column}' not in '{action.table}'."
        if action.values:
            for col in action.values.keys():
                if not column_exists(table_info, col):
                    return f"Column '{col}' in values not in '{action.table}'."
        for col in action.group_by:
            if not column_exists(table_info, col):
                return f"Group-by column '{col}' not in '{action.table}'."
        if action.action in ("update", "delete") and not action.filters:
            return f"Safety: {action.action.upper()} has no WHERE filters."

        # Semantic sanity check: if the request clearly asks for aggregation
        # but the agent produced a plain SELECT with no GROUP BY / HAVING,
        # reject it rather than running a wrong-but-valid query.
        if (
            action.action == "select"
            and not action.group_by
            and not action.having
            and _looks_like_aggregation_request(request)
        ):
            logger.warning(
                "Semantic check: request looks like aggregation, but no "
                "group_by/having provided."
            )
            return (
                "Semantic check: request implies aggregation (e.g. 'more than N "
                "per group') but action has no group_by / having. Agent should "
                "either set action='clarify' or include group_by/having."
            )
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

    def _execute_action(
        self, action: ProposedAction
    ) -> tuple[str | None, str | None, list | None]:
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

    def _result(
        self, raw_action, sql, executed, approval_required, approved,
        rows, error, clarification=None,
    ):
        return {
            "raw_action": raw_action,
            "sql": sql,
            "executed": executed,
            "approval_required": approval_required,
            "approved": approved,
            "rows": rows,
            "error": error,
            "clarification": clarification,
        }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

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
