from __future__ import annotations

from dataclasses import asdict

from ..config import Settings, code_fingerprint
from ..execution.orders import OrderManager
from ..execution.risk import RiskManager
from ..market.session import TICKS, Session
from ..models import Event, Instrument, OrderState, Side
from ..replay.simulator import FillSimulator
from ..storage.ledger import Ledger
from ..strategy.engine import StrategyEngine
from ..strategy.features import FeatureEngine


class TradingCore:
    """One serial account state path, shared unchanged by replay, shadow, mock and live."""

    def __init__(
        self, settings: Settings, session: Session, instruments: dict[str, Instrument], ledger: Ledger
    ):
        self.settings, self.session, self.instruments, self.ledger = settings, session, instruments, ledger
        self.orders = OrderManager(ledger, settings, session.trading_date)
        self.risk = RiskManager(ledger, settings, session.trading_date)
        self.strategy = StrategyEngine(settings.strategy, session)
        self.features: dict[str, FeatureEngine] = {}
        self.simulator = (
            FillSimulator(self.orders, settings, session) if settings.mode in {"replay", "shadow"} else None
        )
        self.sequence = 0
        self.origin_mono = None
        self.origin_wall = None
        self.last_mono = -1
        self.now = 0.0
        self.connected = False
        self.market_phase = "UNKNOWN"
        self.vi: set[str] = set()
        self.vi_release: dict[str, str] = {}
        self.quality_block = False
        self.event_count = 0
        self.invalid_count = 0
        self.gaps = 0
        self.commands: list[tuple[str, str]] = []
        self.signals: list[dict] = []
        self.equity_curve: list[tuple[float, float]] = []
        previous_hash = ledger.get("settings_hash")
        if previous_hash and previous_hash != settings.fingerprint:
            raise ValueError("existing ledger belongs to a different configuration")
        ledger.set("settings_hash", settings.fingerprint)
        ledger.set("code_hash", code_fingerprint())
        if (
            self.simulator
            and not ledger.orders()
            and not ledger.get("daily_halt", False)
            and not ledger.get("operator_halt", False)
        ):
            ledger.set("account_state", "RUNNING")

    def feature(self, symbol: str) -> FeatureEngine:
        feature = self.features.get(symbol)
        if feature is None:
            feature = self.features[symbol] = FeatureEngine(self.settings.data, self.settings.strategy)
        return feature

    def block(self, reason: str):
        self.quality_block = True
        self.orders.halt(reason)
        self.gaps += 1
        for symbol, feature in self.features.items():
            feature.reset(self.now)
            self.strategy.reset(symbol)

    def marks(self) -> dict[str, float]:
        return {
            s: f.book.bids[0].price
            for s, f in self.features.items()
            if f.book and f.book.valid and self.now - f.book_at <= self.settings.data.book_fresh_seconds
        }

    def process(self, event: Event) -> list[tuple[str, str]]:
        if event.trading_date != self.session.trading_date:
            raise ValueError("event trading day differs from session")
        if event.sequence <= self.sequence or event.monotonic_ns < self.last_mono:
            raise ValueError("events must retain receive order")
        if self.origin_mono is None:
            self.origin_mono = event.monotonic_ns
            self.origin_wall = event.received_at.timestamp()
        self.now = self.origin_wall + (event.monotonic_ns - self.origin_mono) / 1e9
        self.last_mono, self.sequence = event.monotonic_ns, event.sequence
        self.event_count += 1
        self.commands = []
        if event.data.get("capture_gap") or event.data.get("processing_lag_block"):
            self.block("recorded_data_quality_failure")
        if not event.valid:
            self.invalid_count += 1
        if event.kind == "connection":
            self.connected = event.data.get("status") == "connected"
            self.market_phase = "UNKNOWN"
            self.block("connection_generation_change")
            if self.connected and self.simulator and not event.data.get("capture_gap"):
                self.quality_block = False
                if not self.ledger.get("daily_halt", False) and not self.ledger.get("operator_halt", False):
                    self.ledger.set("account_state", "RUNNING")
        elif event.kind == "market":
            self.market_phase = event.data.get("phase", "UNKNOWN")
            if self.market_phase != "CONTINUOUS":
                for symbol, feature in self.features.items():
                    feature.reset(self.now)
                    self.strategy.reset(symbol)
        elif event.kind == "vi":
            if event.data.get("active", True):
                self.vi.add(event.symbol)
                self.vi_release[event.symbol] = event.data.get("release_time", "")
            else:
                self.vi.discard(event.symbol)
            self.feature(event.symbol).reset(self.now)
            self.strategy.reset(event.symbol)
        elif event.kind == "universe":
            active = set(event.data.get("symbols", []))
            protected = {p.symbol for p in self.ledger.positions() if p.quantity} | {
                o.symbol for o in self.ledger.orders(active=True)
            }
            for symbol in set(self.features) - active - protected:
                del self.features[symbol]
                self.strategy.reset(symbol)
        elif event.kind == "quality":
            if event.symbol:
                self.feature(event.symbol).reset(self.now)
                self.strategy.reset(event.symbol)
            elif event.data.get("blocking", False):
                self.block("capture_quality_failure")
        if event.symbol and event.kind in {"book", "trade"}:
            feature = self.feature(event.symbol)
            if not event.valid:
                feature.reset(self.now)
                self.strategy.reset(event.symbol)
            elif event.kind == "book":
                if (
                    feature.book is not None
                    and self.now - feature.book_at > self.settings.data.book_fresh_seconds
                ):
                    self.strategy.reset(event.symbol)
                feature.on_book(self.now, event.book)
            elif event.kind == "trade":
                release = self.vi_release.get(event.symbol, "")
                # VI type is not a release flag. Require a subsequent trade AND fresh book after the supplied release time.
                if (
                    event.symbol in self.vi
                    and release
                    and event.exchange_time
                    and event.exchange_time > release
                    and feature.book
                    and feature.book.valid
                    and self.now - feature.book_at <= 2
                    and self.session.phase(self.now) == "CONTINUOUS"
                    and self.market_phase == "CONTINUOUS"
                ):
                    self.vi.discard(event.symbol)
                    feature.reset(self.now)
                    self.strategy.reset(event.symbol)
                    self.ledger.audit("vi_release_evidence", {"symbol": event.symbol, "at": self.now})
                if event.symbol not in self.vi:
                    feature.on_trade(self.now, event.trade)
            feature.suspended = event.symbol in self.vi or self.market_phase != "CONTINUOUS"
        if self.simulator:
            self.simulator.process(
                event,
                self.now,
                self.connected and event.symbol not in self.vi and self.market_phase == "CONTINUOUS",
            )
        if self.settings.mode == "collect":
            return []
        marks = self.marks()
        if event.kind == "timer":
            for symbol, feature in self.features.items():
                stale = (
                    feature.book is not None and self.now - feature.book_at > self.settings.data.alarm_seconds
                )
                previous = self.ledger.get(f"stale_alarm:{symbol}", False)
                if stale and not previous:
                    self.ledger.audit(
                        "stale_quote",
                        {
                            "at": self.now,
                            "symbol": symbol,
                            "age": self.now - feature.book_at,
                            "connected": self.connected,
                            "market_phase": self.market_phase,
                        },
                    )
                if stale != previous:
                    self.ledger.set(f"stale_alarm:{symbol}", stale)
        net = self.risk.mark(marks)
        if event.kind == "timer" and net is not None:
            self.equity_curve.append((self.now, self.ledger.get("start_equity") + net))
        emergency = self.ledger.get("daily_halt", False) or self.ledger.get("emergency", False)
        exit_all = emergency or self.session.exit_due(self.now)
        cancel_entries = exit_all or self.ledger.get("operator_halt", False)
        # Cancel stale entry remainders, or existing orders before repricing.
        for order in self.ledger.orders(active=True):
            age = self.now - order.acknowledged_at if order.acknowledged_at is not None else 0
            cancel = (order.side == Side.BUY and (cancel_entries or age >= 2)) or (
                order.side == Side.SELL and age >= 2 and self.session.phase(self.now) == "CONTINUOUS"
            )
            if cancel and self.orders.request_cancel(order.intent_id, self.now):
                self.commands.append(("cancel", order.intent_id))
            if (
                order.state in {OrderState.SENT, OrderState.UNKNOWN, OrderState.CANCEL_PENDING}
                and self.now - (order.cancel_requested_at or order.created_at) > 10
            ):
                self.orders.halt("order_status_timeout")
        symbols = (
            set(self.features)
            if event.kind in {"timer", "market", "connection", "vi", "order"}
            else ({event.symbol} if event.symbol else set())
        )
        symbols |= {p.symbol for p in self.ledger.positions() if p.quantity and exit_all}
        for symbol in sorted(symbols):
            snap = self.feature(symbol).snapshot(self.now)
            if not self.connected or symbol in self.vi or self.market_phase != "CONTINUOUS":
                snap.valid_book = snap.trade_fresh = snap.ready = False
            position = self.ledger.position(symbol)
            if position.quantity:
                reasons = self.strategy.exit(position, snap, emergency)
                position.exits = list(dict.fromkeys([*position.exits, *reasons]))
                self.ledger.put_position(position)
                if position.exits:
                    for pending in self.ledger.orders(active=True):
                        if (
                            pending.symbol == symbol
                            and pending.side == Side.BUY
                            and self.orders.request_cancel(pending.intent_id, self.now)
                        ):
                            self.commands.append(("cancel", pending.intent_id))
                    instrument = self.instruments.get(symbol)
                    valid_price = (
                        snap.valid_book
                        and TICKS.legal(snap.bid)
                        and instrument
                        and instrument.lower_limit <= snap.bid <= instrument.upper_limit
                    )
                    if valid_price and self.session.phase(self.now) == "CONTINUOUS":
                        order = self.orders.create_exit(symbol, snap.bid, self.now, position.exits)
                        if order:
                            self._signal("exit", symbol, snap, order.intent_id, position.exits)
                            self.commands.append(("submit", order.intent_id))
                continue
            active = any(o.symbol == symbol for o in self.ledger.orders(active=True))
            price = self.strategy.entry(
                symbol, snap, active, event.kind == "trade" and event.symbol == symbol
            )
            if price and not self.quality_block and symbol in self.instruments:
                broker_qty = None
                if self.settings.mode in {"mock", "live"}:
                    snapshot_at = self.ledger.get("broker_snapshot_at", 0)
                    if self.now - snapshot_at > 30:
                        self.orders.halt("stale_account_snapshot")
                        continue
                    broker_qty = int(
                        self.ledger.get("broker_orderable_cash", 0)
                        / (price * (1 + self.settings.costs.buy_fee))
                    )
                order, reason = self.risk.reserve_entry(
                    self.instruments[symbol], price, snap.volume60, self.now, marks, broker_qty
                )
                if order:
                    self._signal("entry", symbol, snap, order.intent_id, [reason])
                    self.commands.append(("submit", order.intent_id))
                else:
                    self.ledger.audit("entry_block", {"at": self.now, "symbol": symbol, "reason": reason})
            state = self.strategy.entries.get(symbol)
            if state:
                previous = self.ledger.get(f"candidate:{symbol}")
                current = {"stage": state.stage, "reason": state.reason}
                if current != previous:
                    self.ledger.audit("candidate_transition", {"at": self.now, "symbol": symbol, **current})
                    self.ledger.set(f"candidate:{symbol}", current)
        if self.simulator:
            for action, intent in self.commands:
                if action == "submit":
                    self.simulator.submit(self.ledger.order(intent), self.now, event.sequence)
                else:
                    self.simulator.cancel(intent, self.now)
        return list(self.commands)

    def _signal(self, action, symbol, snap, intent, reasons):
        record = {
            "at": self.now,
            "sequence": self.sequence,
            "action": action,
            "symbol": symbol,
            "intent_id": intent,
            "reasons": reasons,
            "features": asdict(snap),
            "settings_hash": self.settings.fingerprint,
        }
        if snap.trade:
            record["features"]["trade"] = snap.trade.model_dump(mode="json")
        self.signals.append(record)
        self.ledger.audit("signal", record)
