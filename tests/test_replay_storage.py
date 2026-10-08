from datetime import UTC, datetime, time

import pytest
from conftest import make_book

from korstocktrade.app.core import TradingCore
from korstocktrade.broker.kiwoom.mapping import Normalizer
from korstocktrade.market.session import KST
from korstocktrade.models import Order, Side
from korstocktrade.replay.demo import demo_dataset
from korstocktrade.replay.evaluate import (
    bootstrap_daily_lower,
    date_split,
    maximum_drawdown,
    purge_cross_boundary,
)
from korstocktrade.replay.runner import replay
from korstocktrade.replay.simulator import FillSimulator
from korstocktrade.storage.events import Recorder, parquet_summary, read_events, to_parquet
from korstocktrade.storage.lock import AccountLock


def test_end_to_end_replay_is_deterministic(tmp_path, settings):
    path = tmp_path / "events.jsonl"
    session, instruments = demo_dataset(path)
    first = replay(path, settings, session, instruments)
    second = replay(path, settings, session, instruments)
    assert first == second
    assert first["round_trips"] >= 1 and first["orders"] >= 2
    assert not first["closing_handoff_required"]
    assert first["qualification"] == "UNVALIDATED"
    assert first["quality"]["events"] > 3000


def test_parquet_preserves_normalized_raw_events(tmp_path, settings):
    path = tmp_path / "events.jsonl"
    demo_dataset(path)
    target = tmp_path / "events.parquet"
    count = to_parquet(path, target, batch_size=100)
    summary = parquet_summary(target)
    assert count == sum(row["events"] for row in summary)
    assert {row["kind"] for row in summary} == {"connection", "market", "book", "trade", "timer"}


def test_no_fill_before_latency_or_in_auction(manager, settings, session, day, now):
    simulator = FillSimulator(manager, settings, session)
    order = Order(
        intent_id="buy",
        trading_date=day,
        account="research",
        symbol="005930",
        side=Side.BUY,
        quantity=1000,
        limit_price=10010,
        created_at=now,
    )
    manager.ledger.put_order(order)
    simulator.submit(order, now, 1)
    normalizer = Normalizer("test", day)

    def event(at):
        return normalizer.event(
            "book",
            datetime.fromtimestamp(at, UTC),
            int((at - now) * 1e9),
            symbol="005930",
            book=make_book(ask_qty=100),
        )

    simulator.process(event(now + 0.1), now + 0.1)
    assert manager.ledger.position("005930").quantity == 0
    simulator.process(event(now + 0.3), now + 0.3)
    assert manager.ledger.position("005930").quantity == 25
    auction = datetime.combine(day, time(15, 20), KST).timestamp()
    simulator.process(event(auction), auction)
    assert manager.ledger.position("005930").quantity == 25


def test_receive_order_and_truncated_log_fail_closed(tmp_path, day):
    normalizer = Normalizer("one", day)
    event = normalizer.event("timer", datetime.now(UTC), 1)
    path = tmp_path / "log.jsonl"
    with Recorder(path) as recorder:
        recorder.write(event)
        with pytest.raises(ValueError, match="sequence"):
            recorder.write(event)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"broken":')
    with pytest.raises(ValueError, match="line 2"):
        list(read_events(path))


def test_os_account_lock_released_after_close(tmp_path):
    path = tmp_path / "account.lock.runtime"
    with AccountLock(path), pytest.raises(RuntimeError, match="active process"), AccountLock(path):
        pass
    with AccountLock(path):
        pass


def test_vi_blocks_simulated_execution_and_clears_timers(settings, session, instrument, ledger, day, now):
    core = TradingCore(settings, session, {instrument.symbol: instrument}, ledger)
    normalizer = Normalizer("test", day)
    at = datetime.fromtimestamp(now, UTC)
    core.process(normalizer.event("book", at, 0, symbol=instrument.symbol, book=make_book()))
    core.process(normalizer.event("vi", at, 1, symbol=instrument.symbol, data={"active": True}))
    core.process(normalizer.event("book", at, 2, symbol=instrument.symbol, book=make_book()))
    assert instrument.symbol in core.vi
    assert not core.feature(instrument.symbol).snapshot(core.now).valid_book


def test_statistics_chronological_split_purge_and_insufficient_bootstrap():
    assert date_split([f"2026-10-{day:02}" for day in range(1, 11)]) == {
        "development": [f"2026-10-{day:02}" for day in range(1, 7)],
        "validation": ["2026-10-07", "2026-10-08"],
        "holdout": ["2026-10-09", "2026-10-10"],
    }
    assert maximum_drawdown([100, 120, 90, 110]) == 0.25
    assert bootstrap_daily_lower({"one": [1, 2]}) is None
    assert bootstrap_daily_lower({str(i): [1, 2, 3] for i in range(20)}, iterations=100) > 0
    assert purge_cross_boundary(
        [{"entry_at": 100, "exit_at": 400}, {"entry_at": 500, "exit_at": 900}], 0, 1000
    ) == [{"entry_at": 500, "exit_at": 900}]


def test_persisted_confirmed_floor_does_not_restore_monotonic_timers(
    manager, settings, session, instrument, now
):
    from korstocktrade.models import Position

    manager.ledger.put_position(
        Position(
            symbol=instrument.symbol,
            quantity=1,
            average_price=10000,
            confirmed_high=10295,
            floor_pct=2.36,
            entries=1,
        )
    )
    core = TradingCore(settings, session, {instrument.symbol: instrument}, manager.ledger)
    assert core.ledger.position(instrument.symbol).floor_pct == 2.36
    assert core.strategy.exits == {}
