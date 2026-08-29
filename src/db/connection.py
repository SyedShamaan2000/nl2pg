"""Database connection management."""
import os
from contextlib import contextmanager
import logging
from typing import Generator

import psycopg2
from psycopg2.extensions import connection

logger = logging.getLogger(__name__)


class DatabaseError(Exception):
    """Base exception for database errors."""
    pass


class ConnectionError(DatabaseError):
    """Database connection failure."""
    pass


class QueryExecutionError(DatabaseError):
    """SQL query execution failure."""
    pass


def get_connection() -> connection:
    """Create and return a database connection."""
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "nl2pg"),
            user=os.getenv("DB_USER", "syed"),
            password=os.getenv("DB_PASSWORD", "syed123"),
        )
        logger.debug("Database connection established")
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Failed to connect to database: {e}")
        raise ConnectionError(f"Database connection failed: {e}") from e


@contextmanager
def db_connection() -> Generator[connection, None, None]:
    """Context manager for database connections."""
    conn = None
    try:
        conn = get_connection()
        yield conn
    finally:
        if conn is not None:
            conn.close()
            logger.debug("Database connection closed")
