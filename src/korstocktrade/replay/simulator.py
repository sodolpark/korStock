from __future__ import annotations

from ..config import Settings
from ..execution.orders import OrderManager
from ..market.session import TICKS, Session
from ..models import Event, Fill, Order, OrderState, Side


class FillSimulator:
    """Marketable limit orders only. No inference of passive queue position or auction fills."""

    def __init__(self, manager: OrderManager, settings: Settings, session: Session):
        self.manager, self.settings, self.session = manager, settings, session
        self.arrival: dict[str, tuple[float, int]] = {}
        self.cancels: dict[str, float] = {}
        self.execution_sequence = 0

    def submit(self, order: Order, now: float, sequence: int):
        self.manager.sent(order.intent_id)
        self.arrival[order.intent_id] = (now + self.settings.simulation.latency_seconds, sequence)

    def cancel(self, intent_id: str, now: float):
        self.cancels.setdefault(intent_id, now + self.settings.simulation.cancel_latency_seconds)

    def process(self, event: Event, now: float, tradable: bool = True):
        for intent, at in list(self.cancels.items()):
            if now >= at:
                self.manager.canceled(intent, now)
                del self.cancels[intent]
        for intent, (at, _) in self.arrival.items():
            order = self.manager.ledger.order(intent)
            if order.state == OrderState.SENT and now >= at:
                self.manager.acknowledge(intent, f"SIM-{intent}", now)
        if not (
            tradable
            and event.valid
            and event.kind == "book"
            and event.book
            and event.book.valid
            and self.session.phase(now) == "CONTINUOUS"
        ):
            return
        fraction = self.settings.simulation.depth_fraction
        bids = [[level.price, int(level.quantity * fraction)] for level in event.book.bids]
        asks = [[level.price, int(level.quantity * fraction)] for level in event.book.asks]
        for order in self.manager.ledger.orders(active=True):
            arrival = self.arrival.get(order.intent_id)
            if (
                order.symbol != event.symbol
                or not arrival
                or now < arrival[0]
                or event.sequence <= arrival[1]
                or not order.broker_id
            ):
                continue
            levels = asks if order.side == Side.BUY else bids
            for level in levels:
                price, available = level
                if not price or not available or not TICKS.legal(price):
                    continue
                direction = 1 if order.side == Side.BUY else -1
                fill_price = TICKS.step(price, direction * self.settings.simulation.adverse_ticks)
                if (order.side == Side.BUY and fill_price > order.limit_price) or (
                    order.side == Side.SELL and fill_price < order.limit_price
                ):
                    continue
                quantity = min(order.remaining, available)
                if not quantity:
                    break
                costs = self.settings.costs
                rate = costs.buy_fee if order.side == Side.BUY else costs.sell_fee + costs.sell_tax
                self.execution_sequence += 1
                self.manager.fill(
                    Fill(
                        trading_date=order.trading_date,
                        account=order.account,
                        broker_id=order.broker_id,
                        execution_id=str(self.execution_sequence),
                        symbol=order.symbol,
                        side=order.side,
                        quantity=quantity,
                        price=fill_price,
                        at=now,
                        fee=quantity * fill_price * rate,
                    )
                )
                level[1] -= quantity
                order = self.manager.ledger.order(order.intent_id)
                self.manager.ledger.audit(
                    "slippage",
                    {
                        "intent_id": order.intent_id,
                        "at": now,
                        "bps": direction * 10000 * (fill_price / order.limit_price - 1),
                        "quantity": quantity,
                    },
                )
