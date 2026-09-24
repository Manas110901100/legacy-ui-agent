"""All business rules: validation, permissions, confirmations, retries and fault injection.

The UI never touches SQL; every call goes through BankService with the current Session.
"""

from __future__ import annotations

import random
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from bankapp.auth import Session, verify_password
from bankapp.config import Config
from bankapp.errors import (
    AppError,
    ConfirmationRequired,
    NotFoundError,
    TransientError,
    ValidationError,
)

T = TypeVar("T")

CENT = Decimal("0.01")
LARGE_TRANSFER_LIMIT = Decimal("10000")
MAX_TRANSACTION = Decimal("1000000")
DUPLICATE_WINDOW = timedelta(seconds=60)
MAX_ATTEMPTS = 3
ACCOUNT_TYPES = ("Savings", "Checking")

_ACCOUNT_NO_RE = re.compile(r"^ACC\d{6}$")
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,59}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 \-]{6,18}[0-9]$")


def fmt_money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


# --------------------------------------------------------------------------- validation


def parse_amount(text: str, field: str = "amount") -> Decimal:
    raw = text.strip().replace(",", "")
    if not raw:
        raise ValidationError(field, "Amount is required.")
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        raise ValidationError(field, f"'{text.strip()}' is not a valid amount.") from None
    if not amount.is_finite():
        raise ValidationError(field, f"'{text.strip()}' is not a valid amount.")
    if amount <= 0:
        raise ValidationError(field, "Amount must be greater than zero.")
    if amount.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValidationError(field, "Amount can have at most 2 decimal places.")
    if amount > MAX_TRANSACTION:
        raise ValidationError(
            field, f"Amount exceeds the single-transaction maximum of {fmt_money(MAX_TRANSACTION)}."
        )
    return amount.quantize(CENT)


def normalize_account_no(text: str, field: str = "account_no") -> str:
    value = text.strip().upper()
    if not value:
        raise ValidationError(field, "Account number is required.")
    if not _ACCOUNT_NO_RE.match(value):
        raise ValidationError(
            field, f"'{text.strip()}' is not a valid account number (expected format ACC100001)."
        )
    return value


def validate_name(text: str) -> str:
    value = " ".join(text.split())
    if not value:
        raise ValidationError("full_name", "Customer name is required.")
    if not _NAME_RE.match(value):
        raise ValidationError("full_name", "Name must be 2-60 letters (spaces, . ' - allowed).")
    return value


def validate_email(text: str) -> str:
    value = text.strip().lower()
    if not value:
        raise ValidationError("email", "Email is required.")
    if len(value) > 100 or not _EMAIL_RE.match(value):
        raise ValidationError("email", f"'{text.strip()}' is not a valid email address.")
    return value


def validate_phone(text: str) -> str:
    value = text.strip()
    if not value:
        raise ValidationError("phone", "Phone number is required.")
    if not _PHONE_RE.match(value):
        raise ValidationError("phone", "Phone must be 8-20 digits (spaces, dashes, leading + ok).")
    return value


def validate_account_type(text: str) -> str:
    if text not in ACCOUNT_TYPES:
        raise ValidationError(
            "account_type", f"Account type must be one of: {', '.join(ACCOUNT_TYPES)}."
        )
    return text


def validate_description(text: str) -> str:
    value = text.strip()
    if len(value) > 100:
        raise ValidationError("description", "Description must be 100 characters or fewer.")
    return value


# --------------------------------------------------------------------------- read models


@dataclass(frozen=True)
class AccountSummary:
    account_no: str
    customer_name: str
    email: str
    phone: str
    account_type: str
    balance: Decimal
    status: str
    opened_at: str


@dataclass(frozen=True)
class Transaction:
    created_at: str
    txn_type: str
    amount: Decimal
    balance_after: Decimal
    counterparty: str
    description: str


_ACCOUNT_SELECT = """
    SELECT a.id, a.account_no, a.account_type, a.balance, a.status, a.opened_at,
           c.id AS customer_id, c.full_name, c.email, c.phone
    FROM accounts a JOIN customers c ON c.id = a.customer_id
"""


def _to_summary(row: sqlite3.Row) -> AccountSummary:
    return AccountSummary(
        account_no=row["account_no"],
        customer_name=row["full_name"],
        email=row["email"],
        phone=row["phone"],
        account_type=row["account_type"],
        balance=Decimal(row["balance"]),
        status=row["status"],
        opened_at=row["opened_at"],
    )


# --------------------------------------------------------------------------- service


class BankService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = datetime.now,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conn = conn
        self._config = config
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._now = now
        self._clock = clock
        self._lock = threading.Lock()

    # ----- infrastructure -------------------------------------------------------------

    def _simulate_conditions(self) -> None:
        low, high = self._config.latency
        if high > 0:
            self._sleep(self._rng.uniform(low, high))
        if self._config.fault_rate > 0 and self._rng.random() < self._config.fault_rate:
            raise TransientError("The server did not respond in time.")

    def _run(self, op: Callable[[], T]) -> T:
        """Execute op with simulated latency/faults, retrying transient failures."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self._simulate_conditions()
                with self._lock:
                    return op()
            except TransientError as exc:
                if attempt == MAX_ATTEMPTS:
                    raise AppError(
                        f"Service unavailable after {MAX_ATTEMPTS} attempts "
                        f"({exc.message}). Please try again later."
                    ) from exc
                self._sleep(0.25 * 2 ** (attempt - 1))
            except sqlite3.Error as exc:
                raise AppError(f"Database error: {exc}") from exc
        raise AssertionError("unreachable")

    def _timestamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    def _fetch_account(self, account_no: str, label: str = "Account") -> sqlite3.Row:
        row = self._conn.execute(
            _ACCOUNT_SELECT + " WHERE a.account_no = ?", (account_no,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"{label} {account_no} was not found.")
        return row

    @staticmethod
    def _ensure_open(row: sqlite3.Row, field: str = "account_no") -> None:
        if row["status"] != "ACTIVE":
            raise ValidationError(field, f"Account {row['account_no']} is closed.")

    def _duplicate_warning(self, account_id: int, txn_type: str, amount: Decimal) -> str | None:
        since = (self._now() - DUPLICATE_WINDOW).isoformat(timespec="seconds")
        hit = self._conn.execute(
            "SELECT created_at FROM transactions WHERE account_id = ? AND txn_type = ? "
            "AND amount = ? AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (account_id, txn_type, str(amount), since),
        ).fetchone()
        if hit is None:
            return None
        return (
            f"An identical {txn_type.lower().replace('_', ' ')} of {fmt_money(amount)} "
            f"was posted at {hit['created_at'][11:]}.\nThis may be a duplicate."
        )

    def _post(
        self,
        account_id: int,
        txn_type: str,
        amount: Decimal,
        balance_after: Decimal,
        description: str,
        counterparty: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO transactions (account_id, txn_type, amount, balance_after, counterparty, "
            "description, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                account_id,
                txn_type,
                str(amount),
                str(balance_after),
                counterparty,
                description,
                self._timestamp(),
            ),
        )
        self._conn.execute(
            "UPDATE accounts SET balance = ? WHERE id = ?", (str(balance_after), account_id)
        )

    # ----- authentication ---------------------------------------------------------------

    def login(self, username: str, password: str) -> Session:
        username = username.strip()
        if not username:
            raise ValidationError("username", "User name is required.")
        if not password:
            raise ValidationError("password", "Password is required.")

        def op() -> Session:
            row = self._conn.execute(
                "SELECT username, password_hash, salt, role FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None or not verify_password(password, row["password_hash"], row["salt"]):
                raise ValidationError("password", "Invalid user name or password.")
            return Session(
                row["username"], row["role"], self._config.session_timeout, clock=self._clock
            )

        return self._run(op)

    # ----- queries ----------------------------------------------------------------------

    def list_accounts(self, session: Session, query: str = "") -> list[AccountSummary]:
        session.ensure_active()
        session.require("view")
        pattern = f"%{query.strip()}%"

        def op() -> list[AccountSummary]:
            rows = self._conn.execute(
                _ACCOUNT_SELECT + " WHERE a.account_no LIKE ? OR c.full_name LIKE ? "
                "ORDER BY a.account_no",
                (pattern, pattern),
            ).fetchall()
            return [_to_summary(r) for r in rows]

        return self._run(op)

    def get_account(self, session: Session, account_no: str) -> AccountSummary:
        session.ensure_active()
        session.require("view")
        number = normalize_account_no(account_no)
        return self._run(lambda: _to_summary(self._fetch_account(number)))

    def history(
        self, session: Session, account_no: str
    ) -> tuple[AccountSummary, list[Transaction]]:
        session.ensure_active()
        session.require("view")
        number = normalize_account_no(account_no)

        def op() -> tuple[AccountSummary, list[Transaction]]:
            row = self._fetch_account(number)
            txns = self._conn.execute(
                "SELECT * FROM transactions WHERE account_id = ? ORDER BY created_at DESC, id DESC",
                (row["id"],),
            ).fetchall()
            return _to_summary(row), [
                Transaction(
                    t["created_at"],
                    t["txn_type"],
                    Decimal(t["amount"]),
                    Decimal(t["balance_after"]),
                    t["counterparty"] or "",
                    t["description"],
                )
                for t in txns
            ]

        return self._run(op)

    # ----- account maintenance ----------------------------------------------------------

    def create_account(
        self,
        session: Session,
        full_name: str,
        email: str,
        phone: str,
        account_type: str,
        opening_deposit: str,
    ) -> str:
        session.ensure_active()
        session.require("create")
        name = validate_name(full_name)
        mail = validate_email(email)
        tel = validate_phone(phone)
        kind = validate_account_type(account_type)
        deposit = parse_amount(opening_deposit, "opening_deposit")

        def op() -> str:
            last = self._conn.execute("SELECT MAX(account_no) FROM accounts").fetchone()[0]
            number = f"ACC{int(last[3:]) + 1 if last else 100001}"
            now = self._timestamp()
            with self._conn:
                cur = self._conn.execute(
                    "INSERT INTO customers (full_name, email, phone, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, mail, tel, now),
                )
                cur = self._conn.execute(
                    "INSERT INTO accounts (account_no, customer_id, account_type, balance, "
                    "status, opened_at) VALUES (?, ?, ?, '0.00', 'ACTIVE', ?)",
                    (number, cur.lastrowid, kind, now),
                )
                self._post(cur.lastrowid, "DEPOSIT", deposit, deposit, "Opening deposit")
            return number

        return self._run(op)

    def update_customer(
        self, session: Session, account_no: str, full_name: str, email: str, phone: str
    ) -> None:
        session.ensure_active()
        session.require("edit")
        number = normalize_account_no(account_no)
        name = validate_name(full_name)
        mail = validate_email(email)
        tel = validate_phone(phone)

        def op() -> None:
            row = self._fetch_account(number)
            self._ensure_open(row)
            with self._conn:
                self._conn.execute(
                    "UPDATE customers SET full_name = ?, email = ?, phone = ? WHERE id = ?",
                    (name, mail, tel, row["customer_id"]),
                )

        self._run(op)

    def close_account(self, session: Session, account_no: str, confirmed: bool = False) -> None:
        session.ensure_active()
        session.require("close")
        number = normalize_account_no(account_no)

        def op() -> None:
            row = self._fetch_account(number)
            self._ensure_open(row)
            balance = Decimal(row["balance"])
            if not confirmed:
                payout = (
                    f"\nThe remaining balance of {fmt_money(balance)} will be paid out."
                    if balance > 0
                    else ""
                )
                raise ConfirmationRequired(
                    f"Close account {number} ({row['full_name']})?{payout}\n\n"
                    "This action cannot be undone."
                )
            with self._conn:
                if balance > 0:
                    self._post(
                        row["id"],
                        "CLOSING_PAYOUT",
                        balance,
                        Decimal("0.00"),
                        "Account closed - balance paid out",
                    )
                self._conn.execute(
                    "UPDATE accounts SET status = 'CLOSED' WHERE id = ?", (row["id"],)
                )

        self._run(op)

    # ----- money movement ---------------------------------------------------------------

    def deposit(
        self,
        session: Session,
        account_no: str,
        amount: str,
        description: str = "",
        confirmed: bool = False,
    ) -> Decimal:
        session.ensure_active()
        session.require("deposit")
        number = normalize_account_no(account_no)
        value = parse_amount(amount)
        memo = validate_description(description) or "Cash deposit"

        def op() -> Decimal:
            row = self._fetch_account(number)
            self._ensure_open(row)
            if not confirmed and (warning := self._duplicate_warning(row["id"], "DEPOSIT", value)):
                raise ConfirmationRequired(f"{warning}\n\nPost this deposit anyway?")
            new_balance = Decimal(row["balance"]) + value
            with self._conn:
                self._post(row["id"], "DEPOSIT", value, new_balance, memo)
            return new_balance

        return self._run(op)

    def withdraw(
        self,
        session: Session,
        account_no: str,
        amount: str,
        description: str = "",
        confirmed: bool = False,
    ) -> Decimal:
        session.ensure_active()
        session.require("withdraw")
        number = normalize_account_no(account_no)
        value = parse_amount(amount)
        memo = validate_description(description) or "Cash withdrawal"

        def op() -> Decimal:
            row = self._fetch_account(number)
            self._ensure_open(row)
            balance = Decimal(row["balance"])
            if value > balance:
                raise ValidationError(
                    "amount", f"Insufficient funds. Available balance is {fmt_money(balance)}."
                )
            if not confirmed and (
                warning := self._duplicate_warning(row["id"], "WITHDRAWAL", value)
            ):
                raise ConfirmationRequired(f"{warning}\n\nPost this withdrawal anyway?")
            new_balance = balance - value
            with self._conn:
                self._post(row["id"], "WITHDRAWAL", value, new_balance, memo)
            return new_balance

        return self._run(op)

    def transfer(
        self,
        session: Session,
        from_account: str,
        to_account: str,
        amount: str,
        description: str = "",
        confirmed: bool = False,
    ) -> Decimal:
        session.ensure_active()
        session.require("transfer")
        source_no = normalize_account_no(from_account, "from_account")
        target_no = normalize_account_no(to_account, "to_account")
        if source_no == target_no:
            raise ValidationError("to_account", "Cannot transfer to the same account.")
        value = parse_amount(amount)
        if value > LARGE_TRANSFER_LIMIT:
            session.require("transfer_large")
        memo = validate_description(description) or "Funds transfer"

        def op() -> Decimal:
            source = self._fetch_account(source_no, "Source account")
            target = self._fetch_account(target_no, "Destination account")
            self._ensure_open(source, "from_account")
            self._ensure_open(target, "to_account")
            balance = Decimal(source["balance"])
            if value > balance:
                raise ValidationError(
                    "amount", f"Insufficient funds. Available balance is {fmt_money(balance)}."
                )
            if not confirmed:
                notes = []
                if value >= LARGE_TRANSFER_LIMIT:
                    notes.append(
                        f"This is a large transfer of {fmt_money(value)} to "
                        f"{target_no} ({target['full_name']})."
                    )
                if warning := self._duplicate_warning(source["id"], "TRANSFER_OUT", value):
                    notes.append(warning)
                if notes:
                    raise ConfirmationRequired("\n\n".join(notes) + "\n\nProceed with transfer?")
            new_balance = balance - value
            with self._conn:
                self._post(source["id"], "TRANSFER_OUT", value, new_balance, memo, target_no)
                self._post(
                    target["id"],
                    "TRANSFER_IN",
                    value,
                    Decimal(target["balance"]) + value,
                    memo,
                    source_no,
                )
            return new_balance

        return self._run(op)
