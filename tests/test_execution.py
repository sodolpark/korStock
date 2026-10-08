from datetime import timedelta

import pytest

from korstocktrade.execution.orders import OrderManager, ReconciliationRequired
from korstocktrade.execution.reconcile import BrokerSnapshot, Reconciler
from korstocktrade.execution.risk import RiskManager
from korstocktrade.models import Fill, Order, OrderState, Position, Side
from korstocktrade.storage.ledger import Ledger


def buy(manager, now, quantity=10):
    order = Order(
        intent_id="test-buy",
        trading_date=manager.day,
        account="research",
        symbol="005930",
        side=Side.BUY,
        quantity=quantity,
        limit_price=10000,
        created_at=now,
    )
    manager.ledger.put_order(order)
    manager.sent(order.intent_id)
    manager.acknowledge(order.intent_id, "B1", now)
    return order


def execution(manager, now, quantity, execution_id="E1", broker_id="B1", side=Side.BUY, price=10000):
    return Fill(
        trading_date=manager.day,
        account="research",
        broker_id=broker_id,
        execution_id=execution_id,
        symbol="005930",
        side=side,
        quantity=quantity,
        price=price,
        at=now,
        fee=quantity * price * 0.00015,
    )


def test_partial_fill_cancel_race_and_duplicate(manager, now):
    order = buy(manager, now)
    fill = execution(manager, now, 3)
    assert manager.fill(fill)
    assert not manager.fill(fill)
    assert manager.request_cancel(order.intent_id, now + 2)
    assert manager.fill(execution(manager, now + 2.1, 2, "E2"))
    assert manager.ledger.order(order.intent_id).state == OrderState.CANCEL_PENDING
    assert manager.ledger.position("005930").quantity == 5
    assert manager.ledger.position("005930").entries == 1
    sell = manager.create_exit("005930", 9900, now + 2.2, ["stop_loss"])
    assert sell.quantity == 5
    assert manager.create_exit("005930", 9900, now + 2.3, ["session_exit"]) is None
    manager.canceled(order.intent_id, now + 2.4)
    manager.sent(sell.intent_id)
    manager.acknowledge(sell.intent_id, "S1", now + 2.5)
    manager.fill(execution(manager, now + 3, 5, "E3", "S1", Side.SELL, 9900))
    assert manager.ledger.position("005930").quantity == 0
    assert manager.ledger.position("005930").flat_since == now + 3
    assert manager.ledger.position("005930").consecutive_stops == 1


def test_conflicting_fill_rolls_back_and_halts(manager, now):
    buy(manager, now)
    manager.fill(execution(manager, now, 3))
    cash = manager.ledger.get("cash")
    with pytest.raises(ReconciliationRequired, match="conflicting"):
        manager.fill(execution(manager, now, 4))
    assert manager.ledger.position("005930").quantity == 3
    assert manager.ledger.get("cash") == cash
    assert manager.ledger.get("account_state") == "EXIT_ONLY"


def test_late_fill_after_cancel_remains_terminal(manager, now):
    order = buy(manager, now)
    manager.request_cancel(order.intent_id, now + 2)
    manager.canceled(order.intent_id, now + 3)
    manager.fill(execution(manager, now + 3.1, 2))
    assert manager.ledger.order(order.intent_id).state == OrderState.CANCELED
    assert manager.ledger.position("005930").quantity == 2


def test_missing_fill_identity_rejected(manager, now):
    buy(manager, now)
    with pytest.raises(ValueError, match="identity"):
        manager.fill(execution(manager, now, 1, ""))
    assert manager.ledger.position("005930").quantity == 0


def test_uncertain_order_never_resent_after_restart(tmp_path, settings, day, now):
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path)
    manager = OrderManager(ledger, settings, day)
    order = buy(manager, now)
    manager.unknown(order.intent_id, "response lost")
    ledger.close()
    ledger = Ledger(path)
    manager = OrderManager(ledger, settings, day)
    with pytest.raises(ReconciliationRequired, match="never resend"):
        manager.sent(order.intent_id)
    issues = Reconciler(manager).reconcile(BrokerSnapshot(complete=True))
    assert issues
    assert ledger.get("account_state") == "EXIT_ONLY"
    ledger.close()


def test_atomic_reservations_cash_and_exposure(manager, settings, instrument, now):
    risk = RiskManager(manager.ledger, settings, manager.day)
    reserved = 0
    for index in range(4):
        item = instrument.model_copy(update={"symbol": f"00000{index}"})
        order, reason = risk.reserve_entry(item, 10000, 1_000_000, now, {})
        if order:
            reserved += order.quantity * order.limit_price
        else:
            assert reason == "position_limit"
    assert len(manager.ledger.orders()) == 3
    assert reserved <= settings.risk.equity * 0.5
    first = manager.ledger.orders()[0]
    assert (
        risk.reserve_entry(instrument.model_copy(update={"symbol": first.symbol}), 10000, 1_000_000, now, {})[
            0
        ]
        is None
    )


def test_zero_quantity_and_live_one_share_do_not_override_risk(manager, settings, instrument, now):
    risk = RiskManager(manager.ledger, settings, manager.day)
    assert risk.reserve_entry(instrument, 10000, 50, now, {}) == (None, "zero_quantity")
    body = settings.model_dump()
    body.update(mode="live", profile="live_candidate", live_enabled=True)
    body["broker"]["environment"] = "live"
    body["risk"]["max_positions"] = 1
    body["costs"]["verified"] = True
    from korstocktrade.config import Settings

    live = Settings.model_validate(body)
    risk = RiskManager(manager.ledger, live, manager.day)
    assert risk.reserve_entry(instrument, 10000, 50, now, {})[0] is None
    assert risk.reserve_entry(instrument, 10000, 1000, now, {})[0].quantity == 1


def test_daily_loss_latched_across_restart(tmp_path, settings, day, now):
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path)
    manager = OrderManager(ledger, settings, day)
    ledger.put_position(Position(symbol="005930", quantity=1000, average_price=10000))
    ledger.set("cash", 0)
    risk = RiskManager(ledger, settings, day)
    assert risk.mark({"005930": 9800}) < -settings.risk.equity * 0.01
    ledger.close()
    ledger = Ledger(path)
    manager = OrderManager(ledger, settings, day)
    assert ledger.get("daily_halt") is True
    assert "daily_loss_halt" in Reconciler(manager).reconcile(
        BrokerSnapshot(positions={"005930": 1000}, complete=True)
    )
    ledger.close()


def test_reconcile_missing_fill_is_not_guessed(manager, now):
    buy(manager, now)
    issues = Reconciler(manager).reconcile(
        BrokerSnapshot(
            open_orders={"B1": {"symbol": "005930", "remaining": 8}},
            cumulative_fills={"B1": 2},
            positions={"005930": 2},
            complete=True,
        )
    )
    assert "execution_mismatch:test-buy" in issues
    assert manager.ledger.position("005930").quantity == 0


def test_entry_count_only_on_fill_and_cooldown_after_all_pending_gone(manager, settings, instrument, now):
    order = buy(manager, now)
    assert manager.ledger.position("005930").entries == 0
    manager.canceled(order.intent_id, now + 2)
    risk = RiskManager(manager.ledger, settings, manager.day)
    assert risk.reserve_entry(instrument, 10000, 10000, now + 21.99, {})[1] == "cooldown"
    assert risk.reserve_entry(instrument, 10000, 10000, now + 22, {})[0] is not None


def test_next_day_requires_new_ledger(manager, settings, day):
    with pytest.raises(ReconciliationRequired):
        OrderManager(manager.ledger, settings, day + timedelta(days=1))


def test_partial_mock_fill_immediately_becomes_sellable(manager, settings, day, now):
    from korstocktrade.config import Settings

    body = settings.model_dump()
    body["mode"] = "mock"
    mock_manager = OrderManager(manager.ledger, Settings.model_validate(body), day)
    buy(mock_manager, now)
    mock_manager.fill(execution(mock_manager, now, 3))
    assert mock_manager.sellable("005930") == 3
    order = mock_manager.create_exit("005930", 9900, now + 0.1, ["stop_loss"])
    assert order.quantity == 3


def test_operator_halt_is_not_cleared_by_reconciliation(manager):
    manager.ledger.set("operator_halt", True)
    issues = Reconciler(manager).reconcile(BrokerSnapshot(complete=True))
    assert issues == ["operator_halt"]
    assert manager.ledger.get("account_state") == "EXIT_ONLY"


def test_unknown_cancel_not_cleared_merely_by_open_order_match(manager, now):
    order = buy(manager, now)
    manager.unknown(order.intent_id, "cancel response uncertain")
    snapshot = BrokerSnapshot(complete=True, open_orders={"B1": {"symbol": "005930", "remaining": 10}})
    assert "unresolved_intent:test-buy" in Reconciler(manager).reconcile(snapshot)
    assert manager.ledger.get("account_state") == "EXIT_ONLY"
