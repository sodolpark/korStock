from __future__ import annotations

from dataclasses import dataclass, field

from ..broker.kiwoom.mapping import number, symbol_code
from ..models import Fill, OrderState
from .orders import OrderManager


@dataclass
class BrokerSnapshot:
    open_orders: dict[str, dict] = field(default_factory=dict)
    cumulative_fills: dict[str, int] = field(default_factory=dict)
    positions: dict[str, int] = field(default_factory=dict)
    sellable: dict[str, int] = field(default_factory=dict)
    identified_fills: list[Fill] = field(default_factory=list)
    orderable_cash: float = 0
    economic_cash: float | None = None
    equity: float | None = None
    complete: bool = False
    observed_at: float = 0


async def fetch_snapshot(rest, now: float) -> BrokerSnapshot:
    # The snapshot is deliberately serial: outstanding orders, fills, balances, then cash.
    outstanding = await rest.query(
        "ka10075", {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "1"}, priority=0
    )
    filled = await rest.query(
        "ka10076", {"stk_cd": "", "qry_tp": "0", "sell_tp": "0", "ord_no": "", "stex_tp": "1"}, priority=0
    )
    balance = await rest.query("kt00018", {"qry_tp": "2", "dmst_stex_tp": "KRX"}, priority=0)
    deposit = await rest.query("kt00001", {"qry_tp": "3"}, priority=0)
    snapshot = BrokerSnapshot(observed_at=now)
    for page in outstanding:
        for row in page.get("oso", []):
            snapshot.open_orders[row["ord_no"]] = {
                "symbol": symbol_code(row["stk_cd"], "ka10075"),
                "remaining": number(row["oso_qty"]),
                "quantity": number(row["ord_qty"]),
            }
    for page in filled:
        for row in page.get("cntr", []):
            # This API lacks execution ids. It is evidence for comparison, never an additive fill.
            broker_id = row["ord_no"]
            quantity = number(row["cntr_qty"])
            if broker_id in snapshot.cumulative_fills:
                raise ValueError("ambiguous repeated REST execution row; contract confirmation required")
            snapshot.cumulative_fills[broker_id] = quantity
    for page in balance:
        for row in page.get("acnt_evlt_remn_indv_tot", []):
            symbol = symbol_code(row["stk_cd"], "kt00018")
            if row.get("crd_tp", "00") not in {"", "00"}:
                raise ValueError("non-cash position in account")
            snapshot.positions[symbol] = snapshot.positions.get(symbol, 0) + number(row["rmnd_qty"])
            snapshot.sellable[symbol] = snapshot.sellable.get(symbol, 0) + number(row["trde_able_qty"])
    snapshot.orderable_cash = number(deposit[0]["100stk_ord_alow_amt"])
    snapshot.economic_cash = number(deposit[0]["d2_entra"])
    snapshot.equity = number(balance[0]["prsm_dpst_aset_amt"])
    snapshot.complete = True
    return snapshot


class Reconciler:
    def __init__(self, manager: OrderManager):
        self.manager = manager
        self.ledger = manager.ledger

    def reconcile(self, snapshot: BrokerSnapshot) -> list[str]:
        issues = []
        self.ledger.set("account_state", "RECONCILING")
        if not snapshot.complete:
            issues.append("incomplete_broker_snapshot")
        else:
            for fill in snapshot.identified_fills:
                self.manager.fill(fill)
            for order in self.ledger.orders():
                if order.state in {OrderState.CREATED, OrderState.SENT, OrderState.UNKNOWN}:
                    issues.append(f"unresolved_intent:{order.intent_id}")
                remote = snapshot.open_orders.get(order.broker_id)
                if remote and (remote["remaining"] != order.remaining or remote["symbol"] != order.symbol):
                    issues.append(f"order_mismatch:{order.intent_id}")
                if order.active and order.broker_id and remote is None:
                    issues.append(f"missing_order_terminal_evidence:{order.intent_id}")
                if snapshot.cumulative_fills.get(order.broker_id, 0) != order.filled:
                    issues.append(f"execution_mismatch:{order.intent_id}")
            local_ids = {o.broker_id for o in self.ledger.orders() if o.broker_id}
            for unknown in (set(snapshot.open_orders) | set(snapshot.cumulative_fills)) - local_ids:
                issues.append(f"external_order:{unknown}")
            local_positions = {p.symbol: p.quantity for p in self.ledger.positions() if p.quantity}
            remote_positions = {s: q for s, q in snapshot.positions.items() if q}
            if local_positions != remote_positions:
                issues.append("position_mismatch")
            if snapshot.economic_cash is not None:
                if not self.ledger.get("broker_baseline_initialized", False):
                    if not self.ledger.orders() and not remote_positions and snapshot.economic_cash > 0:
                        with self.ledger.transaction():
                            self.ledger.set("cash", snapshot.economic_cash)
                            self.ledger.set("start_equity", snapshot.equity or snapshot.economic_cash)
                            self.ledger.set("broker_baseline_initialized", True)
                    else:
                        issues.append("initial_account_baseline_unresolved")
                else:
                    difference = snapshot.economic_cash - self.ledger.get("cash")
                    self.ledger.set("cash_difference", difference)
                    if abs(difference) > 1:
                        issues.append("cash_difference_requires_fee_or_external_flow_evidence")
            self.ledger.set("broker_orderable_cash", snapshot.orderable_cash)
            self.ledger.set("broker_sellable", snapshot.sellable)
            self.ledger.set("broker_snapshot_at", snapshot.observed_at)
        if self.ledger.get("daily_halt", False):
            issues.append("daily_loss_halt")
        if self.ledger.get("operator_halt", False):
            issues.append("operator_halt")
        self.ledger.set("account_state", "EXIT_ONLY" if issues else "RUNNING")
        self.ledger.audit(
            "reconciliation",
            {
                "at": snapshot.observed_at,
                "issues": issues,
                "positions": snapshot.positions,
                "open_orders": snapshot.open_orders,
                "complete": snapshot.complete,
            },
        )
        return issues
