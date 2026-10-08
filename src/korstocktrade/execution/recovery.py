"""Explicit, auditable recovery using operator-reviewed broker evidence."""

from __future__ import annotations

import math

from ..models import Fill
from .orders import OrderManager, ReconciliationRequired
from .reconcile import BrokerSnapshot, Reconciler


def recover(manager: OrderManager, evidence: dict) -> list[str]:
    if not evidence.get("evidence_reference"):
        raise ValueError("a reference to the reviewed broker evidence is required")
    if (
        evidence.get("trading_date") != str(manager.day)
        or evidence.get("account") != manager.settings.broker.account_alias
    ):
        raise ValueError("recovery evidence account/day mismatch")
    at = float(evidence["observed_at"])
    for binding in evidence.get("bindings", []):
        order = manager.ledger.order(binding["intent_id"])
        if order.broker_id and order.broker_id != binding["broker_id"]:
            raise ReconciliationRequired("cannot change an established broker identity")
        if any(
            binding.get(field) != getattr(order, field)
            for field in ("symbol", "quantity", "limit_price", "side")
        ):
            raise ValueError("reviewed order economics do not match the local intent")
        manager.acknowledge(order.intent_id, binding["broker_id"], at)
    for raw in evidence.get("identified_fills", []):
        manager.fill(Fill.model_validate(raw))
    for terminal in evidence.get("terminal_orders", []):
        order = manager.ledger.order(terminal["intent_id"])
        if terminal.get("filled_quantity") != order.filled:
            raise ReconciliationRequired("terminal evidence omits executions")
        if terminal["state"] == "CANCELED":
            manager.canceled(order.intent_id, at)
        elif terminal["state"] == "REJECTED":
            manager.reject(order.intent_id, "reviewed broker rejection")
        else:
            raise ValueError("terminal state must be CANCELED or REJECTED; FILLED requires actual fills")
    for adjustment in evidence.get("cash_adjustments", []):
        if adjustment["kind"] not in {"deposit", "withdrawal", "fee_correction"}:
            raise ValueError("unsupported cash adjustment")
        key = "cash_adjustment:" + adjustment["id"]
        with manager.ledger.transaction():
            previous = manager.ledger.get(key)
            if previous:
                if previous != adjustment:
                    raise ReconciliationRequired("conflicting cash adjustment identity")
                continue
            amount = float(adjustment["amount"])
            if not math.isfinite(amount):
                raise ValueError("cash adjustment must be finite")
            if (
                adjustment["kind"] == "deposit"
                and amount <= 0
                or adjustment["kind"] == "withdrawal"
                and amount >= 0
            ):
                raise ValueError("cash flow sign does not match its type")
            manager.ledger.set("cash", manager.ledger.get("cash") + amount)
            if adjustment["kind"] in {"deposit", "withdrawal"}:
                manager.ledger.set("net_deposits", manager.ledger.get("net_deposits", 0) + amount)
            else:
                manager.ledger.set("realized", manager.ledger.get("realized", 0) + amount)
            manager.ledger.set(key, adjustment)
            manager.ledger.audit("cash_adjustment", adjustment)
    snapshot = BrokerSnapshot(**evidence["snapshot"])
    if snapshot.identified_fills:
        raise ValueError("use top-level identified_fills for recovery evidence")
    issues = Reconciler(manager).reconcile(snapshot)
    manager.ledger.audit(
        "reviewed_recovery",
        {"evidence_reference": evidence["evidence_reference"], "observed_at": at, "issues": issues},
    )
    return issues
