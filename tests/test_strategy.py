from datetime import datetime, time

import pytest

from korstocktrade.market.session import KST, TICKS, Session
from korstocktrade.models import Position, Side, Trade
from korstocktrade.strategy.engine import StrategyEngine
from korstocktrade.strategy.features import Snapshot


def snap(at, price=10295, **kwargs):
    data = dict(
        at=at,
        ready=True,
        valid_book=True,
        mid=price,
        bid=price - 5,
        ask=price + 5,
        trade_fresh=True,
        trade=Trade(price=price, quantity=10, side=Side.BUY),
        trend=1,
        imbalance_w=0,
    )
    data.update(kwargs)
    return Snapshot(**data)


def test_confirmed_high_profit_floor_and_recovery(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    position = Position(symbol="005930", quantity=1, average_price=10000)
    assert engine.exit(position, snap(now)) == []
    assert engine.exit(position, snap(now + 9.99)) == []
    assert position.floor_pct is None
    engine.exit(position, snap(now + 10))
    assert position.floor_pct == pytest.approx(2.36)
    engine.exit(position, snap(now + 11, 10200))
    assert engine.exit(position, snap(now + 20.99, 10200)) == []
    engine.exit(position, snap(now + 21, 10250))
    assert engine.exit(position, snap(now + 22, 10200)) == []
    assert engine.exit(position, snap(now + 31.99, 10200)) == []
    assert engine.exit(position, snap(now + 32, 10200)) == ["profit_trailing"]


def test_rising_prices_do_not_reset_high_candidate(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    position = Position(symbol="005930", quantity=1, average_price=10000)
    for second in range(11):
        engine.exit(position, snap(now + second, 10295 + second))
    assert position.confirmed_high == 10295
    assert position.floor_pct == pytest.approx(2.36)
    previous = position.floor_pct
    engine.exit(position, snap(now + 11, 10200, trade_fresh=False))
    assert position.floor_pct == previous


def test_activation_at_two_percent_allows_one_point_six_floor(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    p = Position(symbol="005930", quantity=1, average_price=10000)
    engine.exit(p, snap(now, 10200))
    engine.exit(p, snap(now + 10, 10200))
    assert p.floor_pct == pytest.approx(1.6)


def test_independent_and_timer_exactly_21_seconds(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    p = Position(symbol="005930", quantity=1, average_price=10000)
    for second in range(30):
        assert engine.exit(p, snap(now + second, trend=-1, imbalance_w=-0.69)) == []
    assert engine.exit(p, snap(now + 30, trend=-1, imbalance_w=-0.71)) == []
    assert engine.exit(p, snap(now + 50.999, trend=-1, imbalance_w=-0.71)) == []
    assert engine.exit(p, snap(now + 51, trend=-1, imbalance_w=-0.71)) == ["trend_imbalance"]


def test_invalid_data_resets_both_continuous_timers(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    p = Position(symbol="005930", quantity=1, average_price=10000, floor_pct=3)
    engine.exit(p, snap(now, trend=-1, imbalance_w=-0.8))
    engine.exit(p, snap(now + 9, valid_book=False))
    assert engine.exit(p, snap(now + 10, trend=-1, imbalance_w=-0.8)) == []
    assert engine.exit(p, snap(now + 19, trend=-1, imbalance_w=-0.8)) == []
    assert engine.exit(p, snap(now + 20, trend=-1, imbalance_w=-0.8)) == ["profit_trailing"]


def test_bid_stop_independent_of_trade_or_long_window(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    p = Position(symbol="005930", quantity=1, average_price=10000)
    s = snap(now, 20000, bid=9840, trade_fresh=False, trend=None, imbalance_w=None)
    assert engine.exit(p, s) == ["stop_loss"]


@pytest.mark.parametrize(
    "clock, allowed",
    [(time(9, 1, 59), False), (time(9, 2), True), (time(14, 49, 59), True), (time(14, 50), False)],
)
def test_entry_session_boundaries(session, day, clock, allowed):
    assert session.entry_allowed(datetime.combine(day, clock, KST).timestamp()) is allowed


def test_special_session_and_auction(day):
    session = Session(
        trading_date=day,
        open=time(10),
        entry_start=time(10, 2),
        entry_end=time(15, 50),
        exit_start=time(16, 19),
        auction_start=time(16, 20),
        close=time(16, 30),
    )
    assert session.phase(datetime.combine(day, time(16, 20), KST).timestamp()) == "AUCTION"
    assert session.exit_due(datetime.combine(day, time(16, 19), KST).timestamp())


@pytest.mark.parametrize(
    "price,next_price",
    [(1999, 2000), (4995, 5000), (19990, 20000), (49950, 50000), (199900, 200000), (499500, 500000)],
)
def test_tick_boundaries(price, next_price):
    assert TICKS.step(price) == next_price
    assert TICKS.step(next_price, -1) == price
    assert TICKS.legal(price) and TICKS.legal(next_price)


def entry_snapshot(now, **kwargs):
    data = dict(
        at=now,
        ready=True,
        valid_book=True,
        mid=10005,
        bid=10000,
        ask=10010,
        r60=0,
        r10=0,
        turnover60=600_000_000,
        turnover10=130_000_000,
        trades60=120,
        volume60=10000,
        strength20=150,
        unknown_fraction20=0,
        imbalance10=0,
        stable=True,
        breakout=10010,
        trade=Trade(price=10010, quantity=100, side=Side.BUY),
        trade_fresh=True,
    )
    data.update(kwargs)
    return Snapshot(**data)


def test_entry_filters_breakout_and_price_cap(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    assert engine.entry("005930", entry_snapshot(now), is_trade=True) == 10010
    assert engine.entry("005930", entry_snapshot(now + 1), occupied=True, is_trade=True) is None
    assert engine.entry("005930", entry_snapshot(now + 2, strength20=None), is_trade=True) is None
    assert engine.entry("005930", entry_snapshot(now + 3, unknown_fraction20=0.21), is_trade=True) is None
    assert (
        engine.entry("005930", entry_snapshot(now + 4, bid=10040, ask=10050, mid=10045), is_trade=True)
        is None
    )


def test_candidate_expires_and_no_buy_on_timer(settings, session, now):
    engine = StrategyEngine(settings.strategy, session)
    assert engine.entry("005930", entry_snapshot(now)) is None
    assert engine.entry("005930", entry_snapshot(now + 181), is_trade=True) is None
    assert engine.entries["005930"].reason == "candidate_expired"
