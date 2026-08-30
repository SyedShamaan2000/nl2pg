"""Schema-aware, validated NL-to-SQL agent (v4).

Iteration 4 changes vs v3:
  - Pre-insert UNIQUE-constraint check: introspect now reports which columns
    have a UNIQUE constraint, and before executing an INSERT we SELECT
    existing rows on those columns to catch duplicates ahead of time. The
    raw psycopg2 "duplicate key value violates unique constraint" error is
    now a friendly, actionable message — and the failing row is never sent
    to the DB (see eval case W01).
  - Friendly error mapping for DB-side errors: psycopg2 IntegrityError
    messages are translated into one-line human-friendly descriptions
    (unique violation, FK violation, NOT NULL violation, check violation)
    before they reach the caller.
  - NULL/NOT-NULL precheck on INSERT: if the LLM omits a value for a
    NOT NULL column that has no DEFAULT, we fail fast with a clear message
    instead of letting psycopg2 raise a generic error.

Iteration 3 changes vs v2:
  - JOIN support: 'joins' field on ProposedAction lets the agent express
    multi-table queries properly instead of smuggling a subquery into a
    filter value (which used to get quoted as a string literal and break —
    see eval case R03). Aggregation across a join (e.g. "customers with
    more than 10 orders") also relies on this — see R04.
  - Safe SQL literal quoting: _quote_literal used to be Python's repr(),
    which follows Python escaping rules, not SQL's. Replaced with proper
    single-quote doubling.
  - INSERT no longer lets the LLM invent values for auto-generated columns
    (id, created_at, ...). The schema now reports which columns have a DB
    DEFAULT, the prompt tells the LLM to leave those alone, and a
    deterministic backstop strips them from `values` even if the LLM
    ignores the instruction (see eval case W01).

Iteration 2 changes vs v1:
  - Aggregation support: 'group_by' and 'having' fields on ProposedAction.
  - 'clarify' action: agent can ask for clarification instead of guessing.
  - Semantic validation: catches obviously wrong queries before execution.
  - Multi-provider support: Groq and Gemini.
"""

import logging
import os
from typing import Any, Literal

import psycopg2
from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from src.agent.rate_limit import invoke_with_backoff
from src.db.connection import get_connection
from src.db.introspect import (
    column_exists,
    get_table,
    introspect_schema,
    schema_to_text,
)
from src.models.schemas import Filter, ProposedAction, TableInfo

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM Initializer Functions
# ---------------------------------------------------------------------------


def _init_gemini(
    model_name: str = "gemini-3.5-flash", temperature: float = 0.3
) -> ChatGoogleGenerativeAI:
    """Initialize and return a Gemini LLM instance."""
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    if not api_key:
        logger.warning("GOOGLE_API_KEY / GEMINI_API_KEY not set; Gemini LLM calls will fail.")

    return ChatGoogleGenerativeAI(
        model=model_name,
        temperature=temperature,
        google_api_key=api_key if api_key else None,
        max_retries=2,  # Prevent infinite hangs on 429 quota errors
    )


def _init_groq(model_name: str = "openai/gpt-oss-120b", temperature: float = 0.3) -> ChatGroq:
    """Initialize and return a Groq LLM instance."""
    api_key = os.getenv("GROQ_API_KEY") or ""
    if not api_key:
        logger.warning("GROQ_API_KEY not set; Groq LLM calls will fail.")

    return ChatGroq(
        model_name=model_name,
        temperature=temperature,
        groq_api_key=api_key if api_key else None,
        max_retries=2,
    )


def get_llm(
    provider: Literal["groq", "gemini"] = "groq",
    model_name: str | None = None,
    temperature: float = 0.3,
) -> BaseChatModel:
    """Factory function to instantiate an LLM based on provider choice."""
    if provider == "groq":
        target_model = model_name or "openai/gpt-oss-120b"
        return _init_groq(model_name=target_model, temperature=temperature)
    elif provider == "gemini":
        target_model = model_name or "gemini-3.5-flash"
        return _init_gemini(model_name=target_model, temperature=temperature)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}. Choose 'groq' or 'gemini'.")


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
    escaped = str(val).replace("'", "''")
    return f"'{escaped}'"


def _split_qualified(col: str) -> tuple[str | None, str]:
    """Split 'table.column' into (table, column); 'column' -> (None, column)."""
    if "." in col:
        table, _, column = col.partition(".")
        return table, column
    return None, col


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


def _build_from_clause(action: ProposedAction) -> str:
    """Build the FROM clause, including any JOINs. Table/column names here
    have already been checked against the introspected schema in
    _validate_action, so this is not interpolating arbitrary user text."""
    from_clause = action.table
    for j in action.joins:
        keyword = "LEFT JOIN" if j.join_type == "LEFT" else "JOIN"
        from_clause += f" {keyword} {j.table} ON {j.on_left} = {j.on_right}"
    return from_clause


def build_sql(action: ProposedAction) -> str:
    """Build SQL from a ProposedAction. Supports JOINs and aggregation
    (group_by / having) for SELECT.

    SELECT with group_by: SELECT <cols> FROM <table> [JOIN ...] [WHERE ...]
                          GROUP BY <cols> [HAVING ...]
    SELECT without group_by: SELECT <cols> FROM <table> [JOIN ...] [WHERE ...]
    UPDATE / DELETE take the WHERE clause from filters only, and never join
    (see ProposedAction docstring for why).
    """
    table = action.table
    filters = action.filters
    group_by = action.group_by
    having = action.having
    from_clause = _build_from_clause(action)

    where = ""
    if filters:
        clauses = " AND ".join(_render_filter(f) for f in filters)
        where = f" WHERE {clauses}"

    if action.action == "select":
        # With a join in play, "*" would be ambiguous / return columns from
        # every joined table, so scope it to the primary table the request
        # is actually about.
        select_cols = f"{table}.*" if action.joins else "*"
        if group_by:
            group_clause = ", ".join(group_by)
            having_clause = ""
            if having:
                having_clause = " HAVING " + " AND ".join(_render_filter(f) for f in having)
            return (
                f"SELECT {select_cols} FROM {from_clause}{where} "
                f"GROUP BY {group_clause}{having_clause};"
            )
        return f"SELECT {select_cols} FROM {from_clause}{where};"
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


def _strip_default_columns(action: ProposedAction, table_info: TableInfo) -> list[str]:
    """Remove auto-generated columns from an INSERT's values before building SQL.

    The system prompt tells the LLM not to supply these, but this is a
    deterministic backstop: if it does anyway (e.g. inventing a placeholder
    id like "generated-uuid", or a literal string "now()" for a timestamp),
    we drop them here rather than let a bad value reach the DB.

    Only strips columns that both (a) have a DB DEFAULT and (b) are either
    the primary key or a conventional auto-timestamp column name. A
    legitimate, explicit override of some other defaulted column is left
    alone — this is a narrow safety net, not a blanket filter.

    Returns the list of column names that were dropped, for logging.
    """
    if action.action != "insert" or not action.values:
        return []
    auto_timestamp_names = {"created_at", "updated_at"}
    dropped: list[str] = []
    for col_name in list(action.values.keys()):
        col = next((c for c in table_info.columns if c.name.lower() == col_name.lower()), None)
        if col is None or not col.has_default:
            continue
        if col.is_primary_key or col.name.lower() in auto_timestamp_names:
            dropped.append(col_name)
            del action.values[col_name]
    return dropped


def _check_unique_conflicts(action: ProposedAction, table_info: TableInfo) -> str | None:
    """Pre-flight check: return an error string if any INSERT value
    would violate a UNIQUE constraint on the target table.

    For every column the LLM supplied that is flagged is_unique on the
    schema, we SELECT existing rows with that value and report the
    conflict immediately — before we ever hand a statement to Postgres.
    Returns None when no conflicts are found.
    """
    if action.action != "insert" or not action.values:
        return None
    for col_name, value in action.values.items():
        col = next(
            (c for c in table_info.columns if c.name.lower() == col_name.lower()),
            None,
        )
        if col is None or not col.is_unique or value is None:
            continue
        qualified = f"{action.table}.{col_name}"
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM {action.table} WHERE {col_name} = %s LIMIT 1",
                (value,),
            )
            if cur.fetchone() is not None:
                return (
                    f"Unique constraint violation: a row with {qualified}="
                    f"{_quote_literal(value)} already exists in {action.table}. "
                    f"Please use a different value or update the existing row."
                )
    return None


def _friendly_error(exc: Exception) -> str:
    """Return a one-line human-friendly description of a DB execution error."""
    # psycopg2.IntegrityError (UNIQUE / FK / NOT NULL / CHECK violations)
    if isinstance(exc, psycopg2.IntegrityError):
        msg = str(exc).split("\n")[0]
        low = msg.lower()
        if "unique constraint" in low:
            # Extract the column/key hint if present: "...Key (col)=(val)..."
            detail = ""
            parts = str(exc).split("\n")
            if len(parts) > 1 and parts[1].strip().startswith("DETAIL:"):
                detail = " " + parts[1].strip()
            return f"Duplicate value: {msg}.{detail}"
        if "foreign key constraint" in low or "insert or update on table" in low:
            return f"Foreign-key violation: {msg}"
        if "not-null" in low or "null value" in low:
            return f"NOT NULL violation: {msg}"
        if "check constraint" in low:
            return f"Check constraint violation: {msg}"
        return f"Integrity error: {msg}"
    if isinstance(exc, psycopg2.errors.ForeignKeyViolation):
        return f"Foreign-key violation: {exc}"
    if isinstance(exc, psycopg2.errors.UniqueViolation):
        return f"Duplicate value: {exc}"
    if isinstance(exc, psycopg2.errors.NotNullViolation):
        return f"NOT NULL violation: {exc}"
    return str(exc)


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
    def __init__(
        self,
        approval_fn: Any = None,
        provider: Literal["groq", "gemini"] | None = None,
        model_name: str | None = None,
    ) -> None:
        self._approval_fn = approval_fn
        self._tables: list | None = None

        # Fallback to env var or default to Groq
        selected_provider = provider or os.getenv("LLM_PROVIDER", "groq").lower()  # type: ignore[assignment]
        self.llm = get_llm(
            provider=selected_provider,
            model_name=model_name,
            temperature=0.3,
        )

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
                raw_action=None,
                sql=None,
                executed=False,
                approval_required=False,
                approved=None,
                rows=None,
                error=parse_error,
            )

        logger.debug(f"Agent parsed ProposedAction: {raw_action!r}")

        # Clarification short-circuit: agent said it needs more info.
        if raw_action.action == "clarify":
            logger.info("Agent requested clarification (no query executed).")
            return self._result(
                raw_action=raw_action.model_dump(),
                sql=None,
                executed=False,
                approval_required=False,
                approved=None,
                rows=None,
                error=None,
                clarification=raw_action.reasoning,
            )

        validation_error = self._validate_action(raw_action, tables, request)
        if validation_error:
            logger.warning(f"Schema/semantic validation failed: {validation_error}")
            return self._result(
                raw_action=raw_action.model_dump(),
                sql=None,
                executed=False,
                approval_required=False,
                approved=None,
                rows=None,
                error=f"Validation error: {validation_error}",
            )

        # Deterministic backstop: never let an LLM-invented value for an
        # auto-generated column (id, created_at, ...) reach an INSERT.
        if raw_action.action == "insert":
            table_info = get_table(tables, raw_action.table)
            dropped = _strip_default_columns(raw_action, table_info)
            if dropped:
                logger.info(f"Stripped auto-generated column(s) from INSERT values: {dropped}")
            # Pre-flight unique-constraint check: catch duplicates before
            # they ever reach the DB, with an actionable message.
            unique_err = _check_unique_conflicts(raw_action, table_info)
            if unique_err:
                logger.warning(f"Unique constraint precheck failed: {unique_err}")
                return self._result(
                    raw_action=raw_action.model_dump(),
                    sql=None,
                    executed=False,
                    approval_required=True,
                    approved=True,
                    rows=None,
                    error=unique_err,
                )

            # Pre-flight checks for INSERT: required columns (NOT NULL, no DEFAULT)
            for col in table_info.columns:
                if (
                    not col.nullable
                    and not col.has_default
                    and col.name not in (raw_action.values or {})
                ):
                    return self._result(
                        raw_action=raw_action.model_dump(),
                        sql=None,
                        executed=False,
                        approval_required=False,
                        approved=None,
                        rows=None,
                        error=f"Validation error: Missing required column '{col.name}' for INSERT.",
                    )
            dup_error = _check_unique_conflicts(raw_action, table_info)
            if dup_error:
                return self._result(
                    raw_action=raw_action.model_dump(),
                    sql=None,
                    executed=False,
                    approval_required=False,
                    approved=None,
                    rows=None,
                    error=dup_error,
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
                    sql=None,
                    executed=False,
                    approval_required=True,
                    approved=False,
                    rows=None,
                    error="Approval denied.",
                )

        sql, exec_error, rows_result = self._execute_action(raw_action)
        if exec_error:
            logger.error(f"Execution error: {exec_error}")
            return self._result(
                raw_action=raw_action.model_dump(),
                sql=sql,
                executed=False,
                approval_required=is_write,
                approved=is_write,
                rows=None,
                error=exec_error,
            )

        logger.info(f"Action executed successfully: {raw_action.action} on {raw_action.table}")
        return self._result(
            raw_action=raw_action.model_dump(),
            sql=sql,
            executed=True,
            approval_required=is_write,
            approved=is_write,
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
            "Postgres supports GROUP BY, HAVING, JOIN, and subqueries — use "
            "them when the request asks for counts, totals, or comparisons "
            "across groups (e.g. 'customers with more than 10 orders').\n\n"
            "JOINS: if answering the request needs columns from more than "
            "one table (e.g. filtering orders by a customer attribute, or "
            "counting a related table's rows), set 'table' to the primary "
            "table you want rows from, and add an entry to 'joins' for each "
            "other table you need. Each join needs 'table', 'on_left' and "
            "'on_right' as fully qualified 'table.column' references (e.g. "
            "on_left='orders.customer_id', on_right='customers.id'), and "
            "'join_type' ('INNER' or 'LEFT'). Do NOT put a subquery or "
            "another table's name inside a filter value — use 'joins' "
            "instead. Once tables are joined, filters/group_by/having may "
            "reference columns from any joined table as 'table.column'.\n\n"
            "AGGREGATION: set group_by to the grouping columns (e.g. "
            "['customers.id']) and put comparison filters in 'having' with "
            "a column that is a full aggregate expression, e.g. "
            "{'column': 'COUNT(orders.id)', 'operator': '>', 'value': 10}. "
            "Grouping by a table's primary key lets you also select that "
            "table's other columns (Postgres allows this via functional "
            "dependency on the primary key).\n\n"
            "INSERT VALUES: never include a value for a column marked "
            "'DEFAULT' in the schema below — this includes primary keys "
            "like 'id' and timestamp columns like 'created_at' / "
            "'updated_at' — unless the user's request explicitly gives "
            "that exact value. Let the database fill these in. Also, "
            "columns marked 'UNIQUE' in the schema must not duplicate "
            "an existing value; the agent should validate this before "
            "submitting an INSERT.\n\n"
            "If the user's request is genuinely ambiguous or you cannot map "
            "it to a clear query, set action='clarify' and put your "
            "question in the 'reasoning' field; do NOT guess a destructive "
            "action.\n\n"
            "Shape:\n"
            '{"action":"select"|"insert"|"update"|"delete"|"clarify",'
            '"table":"...",'
            '"joins":[{"table":"...","on_left":"table.col","on_right":"table.col","join_type":"INNER"}],'
            '"filters":[{"column":"...","operator":"=","value":"..."}],'
            '"group_by":["col",...],'
            '"having":[{"column":"COUNT(*)","operator":">","value":10}],'
            '"values":{...},"reasoning":"..."}\n\n'
            f"Schema:\n{schema_text}\n\n"
            "Rules: action must be one of the listed literals; "
            "filter operators =, !=, >, <, >=, <=, IN, LIKE; "
            "UPDATE/DELETE require >=1 filter (safety) and never use joins; "
            "INSERT requires non-null values (excluding DEFAULT columns per "
            "above); for SELECT with aggregation, also fill group_by / having."
        )
        try:
            structured_llm = self.llm.with_structured_output(ProposedAction)
            response = invoke_with_backoff(
                structured_llm.invoke,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Request: {request}"},
                ],
            )
            logger.debug(f"LLM raw response: {response!r}")
            return response, None
        except Exception as exc:
            logger.error(f"LLM call failed: {exc}")
            return None, f"LLM error: {exc}"

    def _validate_action(self, action: ProposedAction, tables: list, request: str) -> str | None:
        """Validate action against the live schema + a semantic sanity check."""
        if not action.table:
            return "Action must specify a table."

        table_info = get_table(tables, action.table)
        if table_info is None:
            return f"Table '{action.table}' not found. Available: {[t.name for t in tables]}"

        if action.joins and action.action != "select":
            return "Joins are only supported for SELECT actions."

        # Validate joins and build a lookup of table_name -> TableInfo for
        # every table now "in scope" (the main table plus any join tables),
        # so filters / group_by / having can reference any of them.
        tables_in_scope: dict[str, TableInfo] = {action.table: table_info}
        for j in action.joins:
            join_table_info = get_table(tables, j.table)
            if join_table_info is None:
                return f"Join table '{j.table}' not found. Available: {[t.name for t in tables]}"
            tables_in_scope[j.table] = join_table_info

            for qualified_col, side in ((j.on_left, "on_left"), (j.on_right, "on_right")):
                qualifier, column = _split_qualified(qualified_col)
                if qualifier is None or qualifier not in tables_in_scope:
                    return (
                        f"Join {side}='{qualified_col}' must be qualified as "
                        f"'table.column' referencing a table already in "
                        f"scope ({list(tables_in_scope)})."
                    )
                if not column_exists(tables_in_scope[qualifier], column):
                    return f"Join column '{qualified_col}' does not exist."

        def _check_column(qualified_col: str) -> str | None:
            """Validate a possibly-qualified ('table.column') reference
            against whichever table it names; unqualified names are checked
            against the main table (preserves old single-table behavior)."""
            qualifier, column = _split_qualified(qualified_col)
            target_table = tables_in_scope.get(qualifier) if qualifier else table_info
            if target_table is None:
                return f"Column '{qualified_col}' references unknown table '{qualifier}'."
            if not column_exists(target_table, column):
                return f"Column '{qualified_col}' not in '{target_table.name}'."
            return None

        for f in action.filters:
            err = _check_column(f.column)
            if err:
                return err
        if action.values:
            for col in action.values.keys():
                # Values are always written to the main table — writes
                # never join, see ProposedAction docstring.
                if not column_exists(table_info, col):
                    return f"Column '{col}' in values not in '{action.table}'."
        for col in action.group_by:
            err = _check_column(col)
            if err:
                return err
        for f in action.having:
            # HAVING columns are frequently aggregate expressions, e.g.
            # "COUNT(orders.id)" — those aren't real columns, so only run
            # the existence check on plain (non-expression) references.
            if "(" not in f.column:
                err = _check_column(f.column)
                if err:
                    return err

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
                "Semantic check: request looks like aggregation, but no group_by/having provided."
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
        except psycopg2.IntegrityError as exc:
            # Roll back the transaction so the next query on this connection
            # isn't poisoned by the aborted state.
            try:
                conn.rollback()
            except Exception:
                logger.error("Failed to rollback after integrity error")
            logger.error(f"DB integrity error: {exc}")
            return None, _friendly_error(exc), None
        except Exception as exc:
            logger.error(f"DB execution failed: {exc}")
            return None, f"DB error: {exc}", None

    def _result(
        self,
        raw_action,
        sql,
        executed,
        approval_required,
        approved,
        rows,
        error,
        clarification=None,
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

    # Example 1: Instantiate with Groq (default)
    agent = Agent(approval_fn=cli_approval, provider="groq")

    # Example 2: Instantiate with Gemini
    # agent = Agent(approval_fn=cli_approval, provider="gemini", model_name="gemini-3.5-flash")

    request = input("Enter request: ")
    result = agent.run(request)
    print("\n=== Result ===")
    for k, v in result.items():
        print(f"  {k}: {v}")
