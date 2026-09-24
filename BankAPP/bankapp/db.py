"""SQLite connection and schema. Money is stored as TEXT and handled as Decimal."""

from __future__ import annotations

import sqlite3

from bankapp import seed

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'teller', 'viewer'))
);
CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY,
    full_name  TEXT NOT NULL,
    email      TEXT NOT NULL,
    phone      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id           INTEGER PRIMARY KEY,
    account_no   TEXT NOT NULL UNIQUE,
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    account_type TEXT NOT NULL,
    balance      TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('ACTIVE', 'CLOSED')),
    opened_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    txn_type      TEXT NOT NULL,
    amount        TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    counterparty  TEXT,
    description   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id, created_at);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection usable from the UI worker thread (access is serialised by a lock)."""
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and load the 50 seed records if the database is empty."""
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0:
        with conn:
            seed.populate(conn)
