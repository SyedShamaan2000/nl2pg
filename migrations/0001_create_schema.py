"""
Create schema: customers, orders, order_items

Revision ID: 0001
Revises:
Create Date: 2026-08-30 00:00:00
"""
from yoyo import step

__depends__ = ()


def apply(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled', 'refunded')),
            total_cents INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price_cents INTEGER NOT NULL
        )
    """)


def rollback(conn):
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS order_items CASCADE")
    cursor.execute("DROP TABLE IF EXISTS orders CASCADE")
    cursor.execute("DROP TABLE IF EXISTS customers CASCADE")

steps = [
    step(apply, rollback)
]