from __future__ import annotations

from ..config import Settings
from ..models import TERMINAL, Fill, Order, OrderState, Side
from ..storage.ledger import Ledger


class ReconciliationRequired(RuntimeError):
    pass


class OrderManager:
    def __init__(self, ledger: Ledger, settings: Settings, day):
        self.ledger, self.settings, self.day = ledger, settings, day
        with ledger.transaction():
            if ledger.get("day") is None:
                ledger.set("day", str(day))
                ledger.set("cash", settings.risk.equity)
                ledger.set("start_equity", settings.risk.equity)
                ledger.set("realized", 0.0)
                ledger.set("account_state", "STARTING")
            elif ledger.get("day") != str(day):
                raise ReconciliationRequired("use a new daily ledger after prior-day handoff reconciliation")

    def halt(self, reason: str):
        self.ledger.set("account_state", "EXIT_ONLY")
        self.ledger.audit("halt", {"reason": reason})

    def unknown(self, intent_id: str, reason: str):
        with self.ledger.transaction():
            order = self.ledger.order(intent_id)
            if order.state not in TERMINAL:
                order.state = OrderState.UNKNOWN
                self.ledger.put_order(order)
            self.halt(reason)

    def sent(self, intent_id: str):
        with self.ledger.transaction():
            order = self.ledger.order(intent_id)
            if order.state != OrderState.CREATED:
                raise ReconciliationRequired("intent was already sent; never resend")
            order.state = OrderState.SENT
            self.ledger.put_order(order)

    def acknowledge(self, intent_id: str, broker_id: str, at: float):
        if not broker_id:
            self.unknown(intent_id, "missing broker order number")
            return
        with self.ledger.transaction():
            order = self.ledger.order(intent_id)
            if order.broker_id and order.broker_id != broker_id:
                raise ReconciliationRequired("conflicting broker order id")
            order.broker_id = broker_id
            if order.state in {OrderState.CREATED, OrderState.SENT, OrderState.UNKNOWN}:
                order.state = OrderState.ACKNOWLEDGED
            order.acknowledged_at = order.acknowledged_at or at
            self.ledger.put_order(order)

    def reject(self, intent_id: str, reason: str):
        with self.ledger.transaction():
            order = self.ledger.order(intent_id)
            if order.filled or order.state in TERMINAL:
                raise ReconciliationRequired("rejection conflicts with prior order state")
            order.state = OrderState.REJECTED
            self.ledger.put_order(order)
            self.ledger.audit("rejection", {"intent_id": intent_id, "reason": reason})

    def request_cancel(self, intent_id: str, at: float) -> bool:
        with self.ledger.transaction():
            order = self.ledger.order(intent_id)
            if (
                not order.active
                or order.state in {OrderState.CANCEL_PENDING, OrderState.UNKNOWN, OrderState.SENT}
                or not order.broker_id
            ):
                return False
            order.state = OrderState.CANCEL_PENDING
            order.cancel_requested_at = at
            self.ledger.put_order(order)
            self.ledger.audit("cancel_intent", {"intent_id": intent_id, "at": at})
            return True

    def canceled(self, intent_id: str, at: float):
        with self.ledger.transaction():
            order = self.ledger.order(intent_id)
            if order.state != OrderState.FILLED:
                order.state = OrderState.CANCELED
            self.ledger.put_order(order)
            if self.settings.mode in {"mock", "live"} and order.side == Side.SELL:
                sellable = self.ledger.get("broker_sellable", {})
                sellable[order.symbol] = min(
                    self.ledger.position(order.symbol).quantity,
                    sellable.get(order.symbol, 0) + order.remaining,
                )
                self.ledger.set("broker_sellable", sellable)
            self._flat_if_complete(order.symbol, at)

    def _flat_if_complete(self, symbol: str, at: float):
        position = self.ledger.position(symbol)
        if position.quantity == 0 and not any(o.symbol == symbol for o in self.ledger.orders(active=True)):
            if position.flat_since is None:
                position.flat_since = at
                self.ledger.put_position(position)

    def fill(self, fill: Fill) -> bool:
        key = fill.key
        try:
            with self.ledger.transaction():
                duplicate = self.ledger.db.execute("SELECT body FROM fills WHERE id=?", (key,)).fetchone()
                if duplicate:
                    old = Fill.model_validate_json(duplicate[0])
                    fields = ("symbol", "side", "quantity", "price")
                    if any(getattr(old, name) != getattr(fill, name) for name in fields):
                        raise ReconciliationRequired("conflicting duplicate execution")
                    return False
                order = self.ledger.by_broker_id(fill.broker_id)
                if order is None:
                    raise ReconciliationRequired("unrecognized/external execution")
                if (
                    fill.symbol != order.symbol
                    or fill.side != order.side
                    or fill.account != order.account
                    or fill.trading_date != order.trading_date
                    or fill.quantity > order.remaining
                ):
                    raise ReconciliationRequired("execution does not match order")
                position = self.ledger.position(fill.symbol)
                amount = fill.quantity * fill.price
                cash = self.ledger.get("cash")
                if fill.side == Side.BUY:
                    if position.quantity == 0:
                        if order.filled == 0:
                            position.entries += 1
                        position.opened_at = fill.at
                        position.cycle_realized = 0
                        position.raw_high = fill.price
                        position.confirmed_high = 0
                        position.floor_pct = None
                        if order.filled == 0:
                            position.exits = []
                        position.mfe_pct = position.mae_pct = 0
                    position.average_price = (position.average_price * position.quantity + amount) / (
                        position.quantity + fill.quantity
                    )
                    position.quantity += fill.quantity
                    position.entry_fees += fill.fee
                    position.flat_since = None
                    cash -= amount + fill.fee
                elif fill.side == Side.SELL:
                    if fill.quantity > position.quantity:
                        raise ReconciliationRequired("execution would oversell position")
                    allocated_fee = position.entry_fees * fill.quantity / position.quantity
                    realized = amount - position.average_price * fill.quantity - allocated_fee - fill.fee
                    position.realized += realized
                    position.cycle_realized += realized
                    position.entry_fees = max(0, position.entry_fees - allocated_fee)
                    position.quantity -= fill.quantity
                    cash += amount - fill.fee
                    self.ledger.set("realized", self.ledger.get("realized", 0) + realized)
                    if position.quantity == 0:
                        stop = "stop_loss" in position.exits
                        position.consecutive_stops = position.consecutive_stops + 1 if stop else 0
                        self.ledger.audit(
                            "round_trip",
                            {
                                "symbol": fill.symbol,
                                "entry_at": position.opened_at,
                                "exit_at": fill.at,
                                "net": position.cycle_realized,
                                "mfe_pct": position.mfe_pct,
                                "mae_pct": position.mae_pct,
                                "reentry": position.entries > 1,
                                "reasons": position.exits,
                            },
                        )
                else:
                    raise ReconciliationRequired("unknown execution side")
                order.average_fill = (order.average_fill * order.filled + amount) / (
                    order.filled + fill.quantity
                )
                order.filled += fill.quantity
                # A late fill after a cancel confirmation must not reopen the canceled remainder.
                if order.remaining == 0:
                    order.state = OrderState.FILLED
                elif order.state not in {OrderState.CANCEL_PENDING, OrderState.CANCELED}:
                    order.state = OrderState.PARTIALLY_FILLED
                self.ledger.put_order(order)
                self.ledger.put_position(position)
                if self.settings.mode in {"mock", "live"}:
                    sellable = self.ledger.get("broker_sellable", {})
                    change = fill.quantity if fill.side == Side.BUY else -fill.quantity
                    sellable[fill.symbol] = min(
                        position.quantity, max(0, sellable.get(fill.symbol, 0) + change)
                    )
                    self.ledger.set("broker_sellable", sellable)
                self.ledger.set("cash", cash)
                self.ledger.db.execute("INSERT INTO fills VALUES(?,?)", (key, fill.model_dump_json()))
                self._flat_if_complete(fill.symbol, fill.at)
            return True
        except ReconciliationRequired as exc:
            self.halt(str(exc))
            raise

    def sellable(self, symbol: str) -> int:
        reserved = sum(
            o.remaining for o in self.ledger.orders(active=True) if o.symbol == symbol and o.side == Side.SELL
        )
        available = max(0, self.ledger.position(symbol).quantity - reserved)
        if self.settings.mode in {"mock", "live"}:
            available = min(available, self.ledger.get("broker_sellable", {}).get(symbol, 0))
        return available

    def create_exit(self, symbol: str, price: int, now: float, reasons: list[str]) -> Order | None:
        with self.ledger.transaction():
            position = self.ledger.position(symbol)
            position.exits = list(dict.fromkeys([*position.exits, *reasons]))
            self.ledger.put_position(position)
            quantity = self.sellable(symbol)
            if not quantity:
                return None
            serial = self.ledger.get("intent_sequence", 0) + 1
            self.ledger.set("intent_sequence", serial)
            order = Order(
                intent_id=f"{self.day}-{serial}",
                trading_date=self.day,
                account=self.settings.broker.account_alias,
                symbol=symbol,
                side=Side.SELL,
                quantity=quantity,
                limit_price=price,
                created_at=now,
                reasons=position.exits,
            )
            self.ledger.put_order(order)
            return order
