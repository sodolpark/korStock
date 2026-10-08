from datetime import date, datetime

import httpx
import pytest

from korstocktrade.config import Settings
from korstocktrade.execution.orders import OrderManager
from korstocktrade.market.session import KST, TICKS, Session
from korstocktrade.models import Book, Instrument, Level
from korstocktrade.storage.ledger import Ledger


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    async def denied(*args, **kwargs):
        raise AssertionError("tests must use an explicit HTTP mock transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)


@pytest.fixture
def settings():
    return Settings.load("configs/research.toml")


@pytest.fixture
def day():
    return date(2026, 10, 8)


@pytest.fixture
def now():
    return datetime(2026, 10, 8, 9, 5, tzinfo=KST).timestamp()


@pytest.fixture
def session(day):
    return Session(trading_date=day)


@pytest.fixture
def instrument(day):
    return Instrument(
        symbol="005930",
        board="KOSPI",
        product="COMMON",
        verified_date=day,
        listing_date=date(2000, 1, 1),
        lower_limit=7000,
        upper_limit=13000,
        normal=True,
    )


@pytest.fixture
def ledger():
    value = Ledger()
    yield value
    value.close()


@pytest.fixture
def manager(ledger, settings, day):
    manager = OrderManager(ledger, settings, day)
    ledger.set("account_state", "RUNNING")
    return manager


def make_book(bid=10000, bid_qty=1000, ask_qty=1000):
    return Book(
        bids=[Level(price=TICKS.step(bid, -i), quantity=bid_qty) for i in range(10)],
        asks=[Level(price=TICKS.step(bid, i + 1), quantity=ask_qty) for i in range(10)],
    )
