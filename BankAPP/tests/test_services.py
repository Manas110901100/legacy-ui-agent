from __future__ import annotations

import random
from decimal import Decimal

import pytest

from bankapp.auth import Session
from bankapp.config import Config
from bankapp.errors import (
    AppError,
    ConfirmationRequired,
    NotFoundError,
    PermissionDeniedError,
    SessionExpiredError,
    ValidationError,
)
from bankapp.services import BankService

A, B = "ACC100001", "ACC100002"


def balance(service: BankService, session: Session, number: str) -> Decimal:
    return service.get_account(session, number).balance


# ----- login / session ------------------------------------------------------------------


def test_login_rejects_bad_credentials(service: BankService) -> None:
    with pytest.raises(ValidationError, match="Invalid user name or password"):
        service.login("admin", "wrong")
    with pytest.raises(ValidationError, match="User name is required"):
        service.login("  ", "x")


def test_session_expires_after_idle_timeout(service: BankService, admin: Session, clock) -> None:  # type: ignore[no-untyped-def]
    clock.advance(299)
    service.list_accounts(admin)  # activity resets the idle timer
    clock.advance(301)
    with pytest.raises(SessionExpiredError):
        service.list_accounts(admin)
    admin.touch()  # activity after expiry does not revive the session
    with pytest.raises(SessionExpiredError):
        service.deposit(admin, A, "10")


# ----- not found --------------------------------------------------------------------------


def test_unknown_account_raises_not_found(service: BankService, admin: Session) -> None:
    with pytest.raises(NotFoundError, match="ACC999999"):
        service.get_account(admin, "ACC999999")
    with pytest.raises(NotFoundError, match="Destination account ACC999999"):
        service.transfer(admin, A, "ACC999999", "10")


# ----- permissions ------------------------------------------------------------------------


def test_viewer_is_read_only(service: BankService, viewer: Session) -> None:
    assert len(service.list_accounts(viewer)) == 50
    with pytest.raises(PermissionDeniedError, match="make deposits"):
        service.deposit(viewer, A, "10")
    with pytest.raises(PermissionDeniedError):
        service.create_account(viewer, "Jane Doe", "j@x.com", "555-010-1234", "Savings", "10")


def test_teller_limits(service: BankService, admin: Session, teller: Session) -> None:
    service.deposit(admin, A, "20000")
    with pytest.raises(PermissionDeniedError, match="close accounts"):
        service.close_account(teller, A, confirmed=True)
    with pytest.raises(PermissionDeniedError, match="teller limit"):
        service.transfer(teller, A, B, "10000.01", confirmed=True)
    service.transfer(teller, A, B, "500")


# ----- validation in context ---------------------------------------------------------------


def test_withdraw_insufficient_funds(service: BankService, admin: Session) -> None:
    current = balance(service, admin, A)
    with pytest.raises(ValidationError, match="Insufficient funds") as info:
        service.withdraw(admin, A, str(current + Decimal("0.01")))
    assert info.value.field == "amount"


def test_transfer_to_same_account_rejected(service: BankService, admin: Session) -> None:
    with pytest.raises(ValidationError, match="same account"):
        service.transfer(admin, A, A.lower(), "10")


def test_closed_account_blocks_transactions(service: BankService, admin: Session) -> None:
    service.close_account(admin, A, confirmed=True)
    with pytest.raises(ValidationError, match="closed"):
        service.deposit(admin, A, "10")
    with pytest.raises(ValidationError, match="closed"):
        service.transfer(admin, B, A, "10")


# ----- confirmations ------------------------------------------------------------------------


def test_close_requires_confirmation_then_pays_out(service: BankService, admin: Session) -> None:
    before = balance(service, admin, A)
    with pytest.raises(ConfirmationRequired, match="cannot be undone"):
        service.close_account(admin, A)
    assert service.get_account(admin, A).status == "ACTIVE"
    service.close_account(admin, A, confirmed=True)
    closed, txns = service.history(admin, A)
    assert closed.status == "CLOSED" and closed.balance == 0
    assert txns[0].txn_type == "CLOSING_PAYOUT" and txns[0].amount == before


def test_large_transfer_requires_confirmation(service: BankService, admin: Session) -> None:
    service.deposit(admin, A, "50000")
    a_before, b_before = balance(service, admin, A), balance(service, admin, B)
    with pytest.raises(ConfirmationRequired, match="large transfer"):
        service.transfer(admin, A, B, "15000")
    assert balance(service, admin, A) == a_before  # nothing moved without confirmation
    service.transfer(admin, A, B, "15000", confirmed=True)
    assert balance(service, admin, A) == a_before - 15000
    assert balance(service, admin, B) == b_before + 15000


def test_duplicate_within_window_requires_confirmation(
    service: BankService, admin: Session, now
) -> None:  # type: ignore[no-untyped-def]
    service.deposit(admin, A, "42")
    now.advance(30)
    with pytest.raises(ConfirmationRequired, match="duplicate"):
        service.deposit(admin, A, "42")
    service.deposit(admin, A, "42", confirmed=True)
    now.advance(61)
    service.deposit(admin, A, "42")  # outside the window: no prompt


# ----- money movement -----------------------------------------------------------------------


def test_deposit_withdraw_transfer_update_balances(service: BankService, admin: Session) -> None:
    start = balance(service, admin, A)
    assert service.deposit(admin, A, "100.25") == start + Decimal("100.25")
    assert service.withdraw(admin, A, "0.25") == start + 100
    service.transfer(admin, A, B, "100", "rent")
    assert balance(service, admin, A) == start
    _, txns = service.history(admin, B)
    assert txns[0].txn_type == "TRANSFER_IN" and txns[0].counterparty == A


def test_create_and_update_account(service: BankService, admin: Session) -> None:
    number = service.create_account(
        admin, "Jane Doe", "Jane@Example.com", "555-010-1234", "Checking", "250"
    )
    assert number == "ACC100051"
    service.update_customer(admin, number, "Jane Smith", "jane@example.com", "555-010-9999")
    account = service.get_account(admin, number)
    assert (account.customer_name, account.balance) == ("Jane Smith", Decimal("250.00"))
    with pytest.raises(ValidationError) as info:
        service.create_account(admin, "Jane Doe", "bad", "555-010-1234", "Checking", "250")
    assert info.value.field == "email"


# ----- slowness and app errors ---------------------------------------------------------------


class ScriptedRng(random.Random):
    """random() returns the scripted values; uniform() returns the midpoint."""

    def __init__(self, values: list[float]) -> None:
        super().__init__(0)
        self._values = list(values)

    def random(self) -> float:
        return self._values.pop(0)

    def uniform(self, a: float, b: float) -> float:
        return (a + b) / 2


def test_latency_is_simulated(make_service) -> None:  # type: ignore[no-untyped-def]
    sleeps: list[float] = []
    service = make_service(Config(latency=(1.0, 3.0)), sleeps=sleeps)
    service.login("admin", "admin123")
    assert len(sleeps) == 1 and 1.0 <= sleeps[0] <= 3.0


def test_transient_fault_is_retried(make_service) -> None:  # type: ignore[no-untyped-def]
    sleeps: list[float] = []
    service = make_service(Config(fault_rate=0.5), rng=ScriptedRng([0.1, 0.1, 0.9]), sleeps=sleeps)
    session = service.login("admin", "admin123")  # fails twice, succeeds on 3rd attempt
    assert session.role == "admin"
    assert sleeps == [0.25, 0.5]


def test_persistent_fault_becomes_app_error(make_service) -> None:  # type: ignore[no-untyped-def]
    service = make_service(Config(fault_rate=1.0), rng=ScriptedRng([0.0] * 3))
    with pytest.raises(AppError, match="after 3 attempts"):
        service.login("admin", "admin123")


def test_database_error_becomes_app_error(service: BankService, admin: Session) -> None:
    service._conn.execute("DROP TABLE transactions")
    with pytest.raises(AppError, match="Database error"):
        service.history(admin, A)
