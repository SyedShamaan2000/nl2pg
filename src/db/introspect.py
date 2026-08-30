"""Live schema introspection from Postgres information_schema.

Reads table/column/foreign-key metadata from the running database and returns
it as Pydantic models (TableInfo, ColumnInfo). The agent uses this output
as its schema context — never a hardcoded description — so the agent stays
correct if the schema changes via a new migration.
"""

import logging
from typing import Any

from src.db.connection import get_connection
from src.models.schemas import ColumnInfo, TableInfo

logger = logging.getLogger(__name__)

# Tables to exclude from introspection. yoyo internals and pg_* catalogs are
# not part of the demo schema; the agent must not see them as queryable.
_EXCLUDED_TABLES = {"_yoyo_log", "_yoyo_migration", "_yoyo_version", "yoyo_lock"}


def _fetch_columns(conn: Any) -> dict[str, list[dict[str, Any]]]:
    """Return a mapping of table_name -> list of column dicts from information_schema."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position
            """
        )
        cols_by_table: dict[str, list[dict[str, Any]]] = {}
        for table_name, column_name, data_type, is_nullable, column_default in cur.fetchall():
            cols_by_table.setdefault(table_name, []).append(
                {
                    "name": column_name,
                    "type": data_type,
                    "nullable": is_nullable == "YES",
                    # column_default is non-NULL for SERIAL/IDENTITY columns,
                    # gen_random_uuid()-style defaults, now()-style timestamp
                    # defaults, etc.
                    "has_default": column_default is not None,
                }
            )
        return cols_by_table


def _fetch_primary_keys(conn: Any) -> dict[str, set[str]]:
    """Return a mapping of table_name -> set of primary-key column names."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public'
            """
        )
        pks: dict[str, set[str]] = {}
        for table_name, column_name in cur.fetchall():
            pks.setdefault(table_name, set()).add(column_name)
        return pks


def _fetch_foreign_keys(conn: Any) -> dict[tuple[str, str], tuple[str, str]]:
    """Return a mapping of (table, column) -> (foreign_table, foreign_column)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT tc.table_name, kcu.column_name,
                   ccu.table_name AS foreign_table_name,
                   ccu.column_name AS foreign_column_name
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
              ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage AS ccu
              ON ccu.constraint_name = tc.constraint_name
              AND ccu.constraint_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = 'public'
            """
        )
        fks: dict[tuple[str, str], tuple[str, str]] = {}
        for table_name, column_name, foreign_table, foreign_column in cur.fetchall():
            fks[(table_name, column_name)] = (foreign_table, foreign_column)
        return fks


def introspect_schema() -> list[TableInfo]:
    """Introspect the live Postgres schema and return validated TableInfo objects.

    Excludes yoyo internals and the pg_* catalogs. Returns an empty list if
    the public schema has no user tables — caller must handle that gracefully
    (see code-quality skill: empty/missing input is a first-class case).
    """
    logger.debug("Introspecting live schema from information_schema")
    with get_connection() as conn:
        cols_by_table = _fetch_columns(conn)
        pks = _fetch_primary_keys(conn)
        fks = _fetch_foreign_keys(conn)

    tables: list[TableInfo] = []
    for table_name, raw_cols in sorted(cols_by_table.items()):
        if table_name in _EXCLUDED_TABLES:
            logger.debug(f"Skipping excluded table: {table_name}")
            continue
        pk_cols = pks.get(table_name, set())
        column_infos: list[ColumnInfo] = []
        for col in raw_cols:
            fk = fks.get((table_name, col["name"]))
            column_infos.append(
                ColumnInfo(
                    name=col["name"],
                    type=col["type"],
                    nullable=col["nullable"],
                    is_primary_key=col["name"] in pk_cols,
                    is_foreign_key=fk is not None,
                    foreign_table=fk[0] if fk else None,
                    foreign_column=fk[1] if fk else None,
                    has_default=col["has_default"],
                )
            )
        tables.append(TableInfo(name=table_name, columns=column_infos))

    logger.info(f"Introspected {len(tables)} table(s): {[t.name for t in tables]}")
    return tables


def schema_to_text(tables: list[TableInfo]) -> str:
    """Render TableInfo list as a compact text description for LLM context.

    Format is kept stable and human-readable so the LLM can parse it reliably.
    Includes column types, nullability, PK, FK relationships, and whether a
    column has a DB-side DEFAULT (so the agent knows not to invent a value
    for it, e.g. a generated id or a created_at timestamp).
    """
    if not tables:
        return "(no tables in public schema)"
    lines: list[str] = []
    for t in tables:
        lines.append(f"Table: {t.name}")
        for c in t.columns:
            notes: list[str] = [c.type]
            if c.is_primary_key:
                notes.append("PK")
            if c.is_foreign_key and c.foreign_table and c.foreign_column:
                notes.append(f"FK->{c.foreign_table}.{c.foreign_column}")
            if not c.nullable:
                notes.append("NOT NULL")
            if c.has_default:
                notes.append("DEFAULT")
            lines.append(f"  - {c.name} ({', '.join(notes)})")
        lines.append("")
    return "\n".join(lines).rstrip()


def get_table(tables: list[TableInfo], name: str) -> TableInfo | None:
    """Return a TableInfo by name (case-insensitive), or None if not found."""
    for t in tables:
        if t.name.lower() == name.lower():
            return t
    return None


def column_exists(table: TableInfo, column_name: str) -> bool:
    """Return True if a column with this name exists on the table (case-insensitive)."""
    for c in table.columns:
        if c.name.lower() == column_name.lower():
            return True
    return False


if __name__ == "__main__":
    # Manual smoke test.
    from src.logging_config import logger as _  # noqa: F401  ensure logging is configured

    logging.basicConfig(
        level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    for t in introspect_schema():
        print(t.name, "->", [c.name for c in t.columns])
    print("---")
    print(schema_to_text(introspect_schema()))
