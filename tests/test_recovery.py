import pytest

from korstocktrade.execution.recovery import recover
from korstocktrade.models import Order, OrderState, Side


def test_explicit_broker_evidence_recovers_lost_response_without_resubmission(manager, now):
    order = Order(
        intent_id="lost",
        trading_date=manager.day,
        account="research",
        symbol="005930",
        side=Side.BUY,
        quantity=1,
        limit_price=10000,
        created_at=now,
    )
    manager.ledger.put_order(order)
    manager.sent("lost")
    manager.unknown("lost", "lost response")
    evidence = {
        "trading_date": str(manager.day),
        "account": "research",
        "observed_at": now + 20,
        "evidence_reference": "sanitized broker execution export, test fixture",
        "bindings": [
            {
                "intent_id": "lost",
                "broker_id": "B1",
                "symbol": "005930",
                "side": "BUY",
                "quantity": 1,
                "limit_price": 10000,
            }
        ],
        "identified_fills": [
            {
                "trading_date": str(manager.day),
                "account": "research",
                "broker_id": "B1",
                "execution_id": "E1",
                "symbol": "005930",
                "side": "BUY",
                "quantity": 1,
                "price": 10000,
                "at": now + 1,
                "fee": 1.5,
            }
        ],
        "snapshot": {
            "open_orders": {},
            "cumulative_fills": {"B1": 1},
            "positions": {"005930": 1},
            "complete": True,
        },
    }
    assert recover(manager, evidence) == []
    assert manager.ledger.order("lost").state == OrderState.FILLED
    assert manager.ledger.position("005930").quantity == 1
    assert recover(manager, evidence) == []
    assert manager.ledger.position("005930").quantity == 1


def test_external_cash_flows_separate_from_pnl_and_idempotent(manager, now):
    cash = manager.ledger.get("cash")
    evidence = {
        "trading_date": str(manager.day),
        "account": "research",
        "observed_at": now,
        "evidence_reference": "cash export",
        "cash_adjustments": [{"id": "D1", "kind": "deposit", "amount": 1000000}],
        "snapshot": {"complete": True},
    }
    recover(manager, evidence)
    recover(manager, evidence)
    assert manager.ledger.get("cash") == cash + 1000000
    assert manager.ledger.get("net_deposits") == 1000000
    assert manager.ledger.get("realized") == 0


def test_recovery_refuses_unreviewed_and_mismatched_evidence(manager):
    with pytest.raises(ValueError, match="reference"):
        recover(manager, {})
    with pytest.raises(ValueError, match="mismatch"):
        recover(manager, {"evidence_reference": "x", "account": "other"})
