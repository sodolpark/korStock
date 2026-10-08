from __future__ import annotations

import math

from ..config import Settings
from ..market.session import TICKS
from ..models import Instrument, Order, Side
from ..storage.ledger import Ledger


class RiskManager:
    def __init__(self, ledger: Ledger, settings: Settings, day):
        self.ledger, self.settings, self.day = ledger, settings, day

    def mark(self, bids: dict[str, float]) -> float | None:
        positions = [p for p in self.ledger.positions() if p.quantity]
        if any(p.symbol not in bids for p in positions):
            return None
        costs = self.settings.costs
        equity = self.ledger.get("cash") + sum(
            p.quantity * bids[p.symbol] * (1 - costs.sell_fee - costs.sell_tax) for p in positions
        )
        start = self.ledger.get("start_equity")
        net = equity - start - self.ledger.get("net_deposits", 0.0)
        self.ledger.set("net_pnl", net)
        if net <= -start * self.settings.risk.daily_loss:
            self.ledger.set("daily_halt", True)
            self.ledger.set("account_state", "EXIT_ONLY")
        return net

    def reserve_entry(
        self,
        instrument: Instrument,
        price: int,
        volume60: int,
        now: float,
        marks: dict[str, float],
        broker_quantity: int | None = None,
    ) -> tuple[Order | None, str]:
        with self.ledger.transaction():
            if self.ledger.get("account_state") != "RUNNING" or self.ledger.get("daily_halt", False):
                return None, "account_not_running"
            if (
                not instrument.eligible(self.day)
                or not TICKS.legal(price)
                or not instrument.lower_limit <= price <= instrument.upper_limit
            ):
                return None, "instrument_or_price_invalid"
            position = self.ledger.position(instrument.symbol)
            orders = self.ledger.orders(active=True)
            if position.quantity or any(o.symbol == instrument.symbol for o in orders):
                return None, "position_or_order_exists"
            strategy = self.settings.strategy
            if position.entries >= strategy.max_entries or position.consecutive_stops >= strategy.stop_streak:
                return None, "entry_limit"
            if position.flat_since is not None and now - position.flat_since < strategy.cooldown:
                return None, "cooldown"
            positions = [p for p in self.ledger.positions() if p.quantity]
            if any(p.symbol not in marks for p in positions):
                return None, "missing_position_mark"
            names = {p.symbol for p in positions} | {o.symbol for o in orders if o.side == Side.BUY}
            risk = self.settings.risk
            if len(names) >= risk.max_positions:
                return None, "position_limit"
            e0 = self.ledger.get("start_equity")
            costs = self.settings.costs
            reserved = sum(o.remaining * o.limit_price for o in orders if o.side == Side.BUY)
            exposure = sum(p.quantity * max(marks[p.symbol], p.average_price) for p in positions) + reserved
            cash = self.ledger.get("cash") - reserved * (1 + costs.buy_fee)
            limits = [
                e0 * risk.risk_per_trade / (price * (strategy.stop_loss_pct / 100 + costs.round_trip)),
                e0 * risk.per_name / price,
                volume60 * risk.participation,
                (e0 * risk.portfolio - exposure) / price,
                cash / (price * (1 + costs.buy_fee)),
            ]
            if broker_quantity is not None:
                limits.append(broker_quantity)
            if self.settings.mode == "live":
                limits.append(1)
            quantity = max(0, min(math.floor(q) for q in limits))
            if not quantity:
                return None, "zero_quantity"
            serial = self.ledger.get("intent_sequence", 0) + 1
            self.ledger.set("intent_sequence", serial)
            order = Order(
                intent_id=f"{self.day}-{serial}",
                trading_date=self.day,
                account=self.settings.broker.account_alias,
                symbol=instrument.symbol,
                side=Side.BUY,
                quantity=quantity,
                limit_price=price,
                created_at=now,
                reasons=["entry_v1"],
            )
            self.ledger.put_order(order)
            return order, "accepted"
