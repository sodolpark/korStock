from datetime import UTC, datetime

import pytest

from korstocktrade.broker.kiwoom.mapping import Normalizer, symbol_code
from korstocktrade.models import Side
from korstocktrade.storage.events import redact


def parse(day, api, values, symbol="005930"):
    n = Normalizer("test", day)
    return n.parse(
        {"trnm": "REAL", "data": [{"type": api, "item": symbol, "values": values}]}, datetime.now(UTC), 1, 1
    )[0]


def test_signed_volume_not_signed_price(day):
    event = parse(day, "0B", {"10": "-10000", "15": "+100", "13": "100", "290": "2"})
    assert event.trade.side == Side.BUY and event.trade.price == 10000
    event = parse(day, "0B", {"10": "+10000", "15": "-100"})
    assert event.trade.side == Side.SELL and event.trade.quantity == 100


def test_all_ten_book_levels(day):
    values = {}
    for i in range(10):
        values[str(41 + i)] = str(10010 + i * 10)
        values[str(51 + i)] = str(10000 - i * 10)
        values[str(61 + i)] = str(i + 100)
        values[str(71 + i)] = str(i + 200)
    event = parse(day, "0D", values)
    assert event.valid
    assert event.book.bids[9].price == 9910
    assert event.book.asks[9].quantity == 109
    del values["80"]
    assert not parse(day, "0D", values).valid


@pytest.mark.parametrize("symbol", ["005930_NX", "005930_AL", "A005930"])
def test_market_feed_prefixes_are_not_blindly_stripped(symbol):
    with pytest.raises(ValueError):
        symbol_code(symbol, "0B")
    assert symbol_code("A005930", "kt00018") == "005930"


def test_dynamic_vi_is_not_release(day):
    event = parse(day, "1h", {"9068": "2", "1224": "090702"})
    assert event.kind == "vi" and event.data["active"] is True


def test_secrets_recursively_redacted():
    raw = {
        "token": "secret",
        "headers": {"Authorization": "Bearer x"},
        "data": [{"9201": "123456", "price": 10}],
    }
    result = redact(raw)
    assert result["token"] == "[REDACTED]"
    assert "secret" not in str(result) and "123456" not in str(result) and "Bearer x" not in str(result)
