import pytest
from conftest import make_book

from korstocktrade.models import Side, Trade
from korstocktrade.strategy.features import FeatureEngine


def test_identical_trades_are_preserved(settings, now):
    engine = FeatureEngine(settings.data, settings.strategy)
    engine.on_book(now, make_book())
    trade = Trade(price=10010, quantity=10, side=Side.BUY)
    engine.on_trade(now, trade)
    engine.on_trade(now, trade)
    engine.on_trade(now, Trade(price=10000, quantity=5, side=Side.SELL))
    snap = engine.snapshot(now)
    assert snap.trades60 == 3 and snap.volume60 == 25
    assert snap.strength20 == 400


def test_book_time_weighting_not_message_weighting(settings, now):
    engine = FeatureEngine(settings.data, settings.strategy)
    engine.on_book(now, make_book(bid_qty=900, ask_qty=100))
    for fraction in [0.1, 0.2, 0.3, 0.4]:
        engine.on_book(now + fraction, make_book(bid_qty=900, ask_qty=100))
    engine.on_book(now + 0.5, make_book(bid_qty=100, ask_qty=900))
    engine.advance(now + 1)
    assert engine.seconds[-1].imbalance == pytest.approx(0, abs=1e-9)


def test_carry_forward_stops_at_two_seconds(settings, now):
    engine = FeatureEngine(settings.data, settings.strategy)
    engine.on_book(now, make_book())
    engine.advance(now + 4)
    assert [s.coverage for s in engine.seconds] == [1, 1, 0, 0]
    assert not engine.snapshot(now + 4).valid_book


def test_no_future_bucket_high_and_window_preparation(settings, now):
    engine = FeatureEngine(settings.data, settings.strategy)
    for second in range(131):
        engine.on_book(now + second, make_book())
        engine.on_trade(now + second, Trade(price=10000, quantity=100, side=Side.SELL))
    before = engine.snapshot(now + 130)
    assert before.ready
    assert before.window_effective == 130
    assert before.trend == pytest.approx(0)
    assert before.breakout == 10010
    engine.on_trade(now + 130, Trade(price=10100, quantity=10, side=Side.BUY))
    after = engine.snapshot(now + 130)
    assert after.breakout == before.breakout
    engine.advance(now + 134)
    assert not engine.snapshot(now + 134).valid_book


def test_sell_zero_and_unknown_volume_fail_entry_inputs(settings, now):
    engine = FeatureEngine(settings.data, settings.strategy)
    engine.on_book(now, make_book())
    engine.on_trade(now, Trade(price=10000, quantity=100, side=Side.BUY))
    engine.on_trade(now, Trade(price=10000, quantity=50, side=Side.UNKNOWN))
    snap = engine.snapshot(now)
    assert snap.strength20 is None
    assert snap.unknown_fraction20 == pytest.approx(1 / 3)


def test_return_reference_uses_quote_known_at_exact_lookback(settings, now):
    engine = FeatureEngine(settings.data, settings.strategy)
    engine.on_book(now, make_book(10000))
    engine.on_book(now + 0.75, make_book(10100))
    for second in range(1, 12):
        engine.on_book(now + second, make_book(10100))
    snapshot = engine.snapshot(now + 11)
    assert snapshot.r10 == pytest.approx(0)
    # Looking back to the start cannot use the future .75s quote in a bucket average.
    other = FeatureEngine(settings.data, settings.strategy)
    other.on_book(now, make_book(10000))
    other.on_book(now + 0.75, make_book(10100))
    for second in range(1, 11):
        other.on_book(now + second, make_book(10100))
    assert other.snapshot(now + 10).r10 == pytest.approx(100 * (10105 / 10005 - 1))
