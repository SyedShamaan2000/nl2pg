"""
Convert all table IDs from SERIAL to UUID

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-30 00:00:00
"""

from yoyo import step

__depends__ = {"0002_seed_data"}

def apply(conn):
    cursor = conn.cursor()
    # Add UUID extension if not present
    cursor.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";")
    
    # Create new UUID-based customers table, copy data
    cursor.execute("""
        CREATE TABLE customers_new (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cursor.execute("""
        INSERT INTO customers_new (id, name, email, created_at)
        SELECT gen_random_uuid(), name, email, created_at FROM customers
    """)
    
    # Create new UUID-based orders table
    cursor.execute("""
        CREATE TABLE orders_new (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            customer_id UUID NOT NULL REFERENCES customers_new(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled', 'refunded')),
            total_cents INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    cursor.execute("""
        INSERT INTO orders_new (id, customer_id, status, total_cents, created_at)
        SELECT gen_random_uuid(), (SELECT id FROM customers_new WHERE customers_new.email = customers.email), status, total_cents, created_at FROM orders JOIN customers ON orders.customer_id = customers.id
    """)
    
    # Create new UUID-based order_items table
    cursor.execute("""
        CREATE TABLE order_items_new (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            order_id UUID NOT NULL REFERENCES orders_new(id) ON DELETE CASCADE,
            product_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price_cents INTEGER NOT NULL
        )
    """)
    
    # Rename tables (swap)
    cursor.execute("ALTER TABLE customers RENAME TO customers_old")
    cursor.execute("ALTER TABLE orders RENAME TO orders_old")
    cursor.execute("ALTER TABLE order_items RENAME TO order_items_old")
    
    cursor.execute("ALTER TABLE customers_new RENAME TO customers")
    cursor.execute("ALTER TABLE orders_new RENAME TO orders")
    cursor.execute("ALTER TABLE order_items_new RENAME TO order_items")
    
    # Drop old tables
    cursor.execute("DROP TABLE customers_old CASCADE")
    cursor.execute("DROP TABLE orders_old CASCADE")
    cursor.execute("DROP TABLE order_items_old CASCADE")

def rollback(conn):
    cursor = conn.cursor()
    # Note: rollback from UUID back to SERIAL is complex; 
    # this is a simplified rollback that drops UUID tables
    cursor.execute("DROP TABLE IF EXISTS order_items CASCADE")
    cursor.execute("DROP TABLE IF EXISTS orders CASCADE")
    cursor.execute("DROP TABLE IF EXISTS customers CASCADE")

steps = [
    step(apply, rollback)
]