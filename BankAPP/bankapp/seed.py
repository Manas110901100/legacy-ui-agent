"""Deterministic seed data: 3 users and 50 customers, each with one account and history."""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from bankapp.auth import hash_password

RECORD_COUNT = 50
FIRST_ACCOUNT_NO = 100001

SEED_USERS = [
    ("admin", "admin123", "admin"),
    ("teller", "teller123", "teller"),
    ("viewer", "viewer123", "viewer"),
]

FIRST_NAMES = [
    "James",
    "Mary",
    "Robert",
    "Patricia",
    "John",
    "Jennifer",
    "Michael",
    "Linda",
    "David",
    "Elizabeth",
    "William",
    "Barbara",
    "Richard",
    "Susan",
    "Joseph",
    "Jessica",
    "Thomas",
    "Sarah",
    "Charles",
    "Karen",
    "Arjun",
    "Priya",
    "Rahul",
    "Ananya",
    "Wei",
    "Mei",
    "Carlos",
    "Sofia",
    "Ahmed",
    "Fatima",
]
LAST_NAMES = [
    "Smith",
    "Johnson",
    "Williams",
    "Brown",
    "Jones",
    "Garcia",
    "Miller",
    "Davis",
    "Rodriguez",
    "Martinez",
    "Hernandez",
    "Lopez",
    "Wilson",
    "Anderson",
    "Taylor",
    "Moore",
    "Jackson",
    "Martin",
    "Lee",
    "Thompson",
    "Sharma",
    "Patel",
    "Reddy",
    "Chen",
    "Wang",
    "Khan",
    "Nguyen",
]
ACCOUNT_TYPES = ["Savings", "Checking"]
BASE_DATE = datetime(2025, 1, 6, 9, 0, 0)


def _money(rng: random.Random, low: int, high: int) -> Decimal:
    return Decimal(rng.randint(low * 100, high * 100)) / 100


def _ts(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def populate(conn: sqlite3.Connection, count: int = RECORD_COUNT) -> None:
    rng = random.Random(42)

    for username, password, role in SEED_USERS:
        pw_hash, salt = hash_password(password)
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
            (username, pw_hash, salt, role),
        )

    used_names: set[str] = set()
    for i in range(count):
        while True:
            name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
            if name not in used_names:
                used_names.add(name)
                break
        first, last = name.lower().split()
        email = f"{first}.{last}{rng.randint(1, 99)}@example.com"
        phone = f"555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}"
        opened = BASE_DATE + timedelta(days=rng.randint(0, 400), minutes=rng.randint(0, 480))

        cur = conn.execute(
            "INSERT INTO customers (full_name, email, phone, created_at) VALUES (?, ?, ?, ?)",
            (name, email, phone, _ts(opened)),
        )
        account_no = f"ACC{FIRST_ACCOUNT_NO + i}"
        cur = conn.execute(
            "INSERT INTO accounts (account_no, customer_id, account_type, balance, status, "
            "opened_at) VALUES (?, ?, ?, '0.00', 'ACTIVE', ?)",
            (account_no, cur.lastrowid, rng.choice(ACCOUNT_TYPES), _ts(opened)),
        )
        account_id = cur.lastrowid

        balance = _money(rng, 500, 25000)
        entries = [("DEPOSIT", balance, balance, "Opening deposit", opened)]
        moment = opened
        for _ in range(rng.randint(2, 5)):
            moment += timedelta(days=rng.randint(1, 30), minutes=rng.randint(0, 600))
            if rng.random() < 0.55 or balance < 100:
                amount = _money(rng, 20, 5000)
                balance += amount
                entries.append(("DEPOSIT", amount, balance, "Cash deposit", moment))
            else:
                amount = _money(rng, 10, int(balance) // 2)
                balance -= amount
                entries.append(("WITHDRAWAL", amount, balance, "ATM withdrawal", moment))

        conn.executemany(
            "INSERT INTO transactions (account_id, txn_type, amount, balance_after, description, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(account_id, t, str(a), str(b), d, _ts(m)) for t, a, b, d, m in entries],
        )
        conn.execute("UPDATE accounts SET balance = ? WHERE id = ?", (str(balance), account_id))
