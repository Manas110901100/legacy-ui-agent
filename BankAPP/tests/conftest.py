from __future__ import annotations

import random
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import pytest

from bankapp import db
from bankapp.auth import Session
from bankapp.config import Config
from bankapp.services import BankService


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeNow:
    def __init__(self) -> None:
        self.moment = datetime(2026, 9, 23, 10, 0, 0)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def now() -> FakeNow:
    return FakeNow()


@pytest.fixture
def make_service(clock: FakeClock, now: FakeNow) -> Iterator[Callable[..., BankService]]:
    conns = []

    def factory(
        config: Config | None = None,
        rng: random.Random | None = None,
        sleeps: list[float] | None = None,
    ) -> BankService:
        conn = db.connect(":memory:")
        db.init_db(conn)
        conns.append(conn)
        sink = sleeps if sleeps is not None else []
        return BankService(
            conn,
            config or Config(session_timeout=300),
            rng=rng,
            sleep=sink.append,
            now=now,
            clock=clock,
        )

    yield factory
    for conn in conns:
        conn.close()


@pytest.fixture
def service(make_service: Callable[..., BankService]) -> BankService:
    return make_service()


@pytest.fixture
def admin(service: BankService) -> Session:
    return service.login("admin", "admin123")


@pytest.fixture
def teller(service: BankService) -> Session:
    return service.login("teller", "teller123")


@pytest.fixture
def viewer(service: BankService) -> Session:
    return service.login("viewer", "viewer123")
