"""
Seed data: customers, orders, order_items
Revision ID: 0002
Revises: 0001
Create Date: 2026-08-30 00:00:00
"""
from yoyo import step
__depends__ = {"0001_create_schema"}

def apply(conn):
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO customers (name, email, created_at) VALUES
            ('Alice Johnson', 'alice@example.com', now() - interval '365 days'),
            ('Bob Smith', 'bob@example.com', now() - interval '300 days'),
            ('Carol Davis', 'carol@example.com', now() - interval '250 days'),
            ('David Wilson', 'david@example.com', now() - interval '200 days'),
            ('Eve Martinez', 'eve@example.com', now() - interval '150 days'),
            ('Frank Lee', 'frank@example.com', now() - interval '100 days'),
            ('Grace Kim', 'grace@example.com', now() - interval '50 days'),
            ('Henry Brown', 'henry@example.com', now() - interval '30 days'),
            ('Ivy Chen', 'ivy@example.com', now() - interval '20 days'),
            ('Jack White', 'jack@example.com', now() - interval '10 days'),
            ('Karen Adams', 'karen@example.com', now() - interval '5 days'),
            ('Liam Baker', 'liam@example.com', now() - interval '2 days'),
            ('Mia Nelson', 'mia@example.com', now() - interval '1 day'),
            ('Noah Carter', 'noah@example.com', now()),
            ('Olivia Mitchell', 'olivia@example.com', now())
    """)
    cursor.execute("""
        INSERT INTO orders (customer_id, status, total_cents, created_at) VALUES
            (1, 'pending', 1250, now() - interval '10 days'),
            (2, 'shipped', 3200, now() - interval '15 days')
    """)

def rollback(conn):
    pass