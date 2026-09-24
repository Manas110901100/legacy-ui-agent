from __future__ import annotations

from decimal import Decimal

from bankapp.auth import Session
from bankapp.services import BankService


def test_seed_has_50_active_accounts(service: BankService, admin: Session) -> None:
    accounts = service.list_accounts(admin)
    assert len(accounts) == 50
    assert accounts[0].account_no == "ACC100001"
    assert accounts[-1].account_no == "ACC100050"
    assert all(a.status == "ACTIVE" and a.balance > 0 for a in accounts)
    assert len({a.customer_name for a in accounts}) == 50


def test_seed_balances_match_transaction_history(service: BankService, admin: Session) -> None:
    for account in service.list_accounts(admin):
        summary, txns = service.history(admin, account.account_no)
        assert txns, account.account_no
        assert txns[0].balance_after == summary.balance
        assert txns[-1].txn_type == "DEPOSIT"
        total = sum(
            (t.amount if t.txn_type == "DEPOSIT" else -t.amount for t in txns), Decimal("0")
        )
        assert total == summary.balance


def test_seed_is_deterministic(make_service) -> None:  # type: ignore[no-untyped-def]
    first = make_service()
    second = make_service()
    a = first.list_accounts(first.login("admin", "admin123"))
    b = second.list_accounts(second.login("admin", "admin123"))
    assert a == b
