from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from ..broker.kiwoom.mapping import Normalizer
from ..market.session import KST, TICKS, Session
from ..models import Book, Instrument, Level, Side, Trade
from ..storage.events import Recorder


def demo_dataset(path: Path, day: date = date(2026, 10, 8)):
    """Deterministic synthetic input with a stable base, buy breakout, rise, then retracement."""
    session = Session(trading_date=day)
    instrument = Instrument(
        symbol="005930",
        name="SYNTHETIC",
        board="KOSPI",
        product="COMMON",
        verified_date=day,
        listing_date=date(2000, 1, 1),
        lower_limit=7000,
        upper_limit=13000,
        normal=True,
    )
    start = datetime.combine(day, time(9), KST)
    normalizer = Normalizer("synthetic-demo-v1", day)
    cumulative = 0
    with Recorder(path) as recorder:
        recorder.write(normalizer.event("connection", start.astimezone(UTC), 0, data={"status": "connected"}))
        recorder.write(normalizer.event("market", start.astimezone(UTC), 0, data={"phase": "CONTINUOUS"}))
        for second in range(420):
            if second < 130:
                bid = 10000
            elif second < 180:
                bid = 10020 + (second - 130) // 2 * 10
            elif second < 220:
                bid = 10300
            elif second < 245:
                bid = 10290 - (second - 220) * 10
            else:
                bid = 10000
            for quarter in range(4):
                elapsed = second + quarter * 0.25
                at = (start + timedelta(seconds=elapsed)).astimezone(UTC)
                mono = int(elapsed * 1e9)
                book = Book(
                    bids=[Level(price=TICKS.step(bid, -i), quantity=10000) for i in range(10)],
                    asks=[Level(price=TICKS.step(bid, i + 1), quantity=5000) for i in range(10)],
                )
                recorder.write(
                    normalizer.event("book", at, mono, symbol=instrument.symbol, api="synthetic", book=book)
                )
                side = Side.SELL if quarter == 0 else Side.BUY
                quantity = (1600 if 128 <= second < 145 else 400) if side == Side.BUY else 100
                cumulative += quantity
                trade = Trade(
                    price=bid if side == Side.SELL else TICKS.step(bid),
                    quantity=quantity,
                    side=side,
                    cumulative_quantity=cumulative,
                )
                recorder.write(
                    normalizer.event(
                        "trade", at, mono, symbol=instrument.symbol, api="synthetic", trade=trade
                    )
                )
            at = (start + timedelta(seconds=second + 1)).astimezone(UTC)
            recorder.write(normalizer.event("timer", at, (second + 1) * 1_000_000_000))
        # A closing timer makes remaining inventory an explicit handoff, never a fictitious fill.
        at = datetime.combine(day, time(15, 30), KST).astimezone(UTC)
        recorder.write(normalizer.event("timer", at, int((at - start).total_seconds() * 1e9)))
    return session, {instrument.symbol: instrument}
