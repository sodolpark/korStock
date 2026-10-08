from __future__ import annotations

from dataclasses import dataclass, field

from ..config import StrategyConfig
from ..market.session import TICKS, Session
from ..models import Position, Side
from .features import Snapshot


@dataclass
class ContinuousTimer:
    since: float | None = None

    def check(self, condition: bool, now: float, duration: float) -> bool:
        if not condition:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= duration


@dataclass
class EntryState:
    stage: str = "WARMING"
    started: float | None = None
    stabilized: float | None = None
    reason: str = "warming"

    def reset(self, ready: bool, reason: str):
        self.stage = "READY" if ready else "WARMING"
        self.started = self.stabilized = None
        self.reason = reason


@dataclass
class ExitTimers:
    high: ContinuousTimer = field(default_factory=ContinuousTimer)
    trailing: ContinuousTimer = field(default_factory=ContinuousTimer)
    trend: ContinuousTimer = field(default_factory=ContinuousTimer)
    candidate: float | None = None

    def reset(self):
        self.high.since = self.trailing.since = self.trend.since = None
        self.candidate = None


class StrategyEngine:
    def __init__(self, config: StrategyConfig, session: Session):
        self.config, self.session = config, session
        self.entries: dict[str, EntryState] = {}
        self.exits: dict[str, ExitTimers] = {}

    def reset(self, symbol: str):
        self.entries.pop(symbol, None)
        self.exits.pop(symbol, None)

    def entry(
        self, symbol: str, snap: Snapshot, occupied: bool = False, is_trade: bool = False
    ) -> int | None:
        state = self.entries.setdefault(symbol, EntryState())
        c, now = self.config, snap.at
        if occupied:
            state.reset(snap.ready, "position_or_order")
            return None
        common = (
            self.session.entry_allowed(now)
            and snap.ready
            and snap.valid_book
            and snap.turnover60 >= c.min_turnover_60
            and snap.trades60 >= c.min_trades_60
            and snap.ask <= TICKS.step(snap.bid, c.spread_ticks)
            and 100 * (snap.ask - snap.bid) / snap.mid <= c.spread_pct
            and snap.unknown_fraction20 <= 0.20
        )
        downside = (
            snap.r60 is not None
            and snap.r60 >= c.r60_min_pct
            and snap.r10 is not None
            and snap.r10 >= c.r10_min_pct
            and snap.imbalance10 is not None
            and snap.imbalance10 > c.imbalance_stage1
        )
        if not common or not downside:
            state.reset(snap.ready, "common_filter" if not common else "downside_filter")
            return None
        if state.started is not None and now - state.started > c.candidate_ttl:
            state.reset(True, "candidate_expired")
            return None
        if state.started is None:
            state.started, state.stage = now, "STAGE1"
        if state.stabilized is not None and now - state.stabilized > c.stage2_ttl:
            state.stabilized = None
            state.stage = "STAGE1"
        stable = snap.stable and snap.imbalance10 > c.imbalance_stage2
        if not stable:
            state.stabilized = None
            state.stage = "STAGE1"
        elif state.stabilized is None:
            state.stabilized, state.stage = now, "STAGE2"
        if state.stabilized is None:
            state.reason = "stabilizing"
            return None
        strength = (
            snap.strength20 is not None
            and snap.strength20 >= c.strength_min
            and snap.turnover60 > 0
            and snap.turnover10 / (snap.turnover60 / 6) >= c.turnover_acceleration
        )
        breakout = (
            is_trade
            and snap.trade_fresh
            and snap.trade is not None
            and snap.trade.side == Side.BUY
            and snap.breakout is not None
            and snap.trade.price >= snap.breakout
        )
        if strength and breakout:
            if snap.ask > TICKS.step(snap.breakout, 2):
                state.reset(True, "price_cap")
                return None
            state.stage, state.reason = "ENTRY_PENDING", "entry_v1"
            return snap.ask
        state.reason = "waiting_for_buy_breakout"
        return None

    def exit(self, position: Position, snap: Snapshot, emergency: bool = False) -> list[str]:
        c, now = self.config, snap.at
        timers = self.exits.setdefault(position.symbol, ExitTimers())
        reasons = []
        if emergency:
            reasons.append("daily_or_emergency")
        if snap.valid_book and 100 * (snap.bid / position.average_price - 1) <= -c.stop_loss_pct:
            reasons.append("stop_loss")
        if self.session.exit_due(now):
            reasons.append("session_exit")
        if not snap.valid_book or self.session.phase(now) != "CONTINUOUS":
            timers.reset()
            return reasons
        mark_return = 100 * (snap.bid / position.average_price - 1)
        position.mfe_pct = max(position.mfe_pct, mark_return)
        position.mae_pct = min(position.mae_pct, mark_return)
        valid_trade = snap.trade_fresh and snap.trade is not None
        if valid_trade:
            price = snap.trade.price
            position.raw_high = max(position.raw_high, price)
            if timers.candidate is None and price > position.confirmed_high:
                timers.candidate = price
                timers.high.since = now
            if timers.candidate is not None:
                if price < timers.candidate * (1 - c.high_tolerance):
                    timers.candidate = None
                    timers.high.since = None
                elif timers.high.check(True, now, c.high_confirm_seconds):
                    position.confirmed_high = max(position.confirmed_high, timers.candidate)
                    gain = 100 * (position.confirmed_high / position.average_price - 1)
                    if gain >= c.trailing_activation_pct:
                        position.floor_pct = max(position.floor_pct or 0.0, c.profit_protection * gain)
                    timers.candidate = price if price > position.confirmed_high else None
                    timers.high.since = now if timers.candidate else None
            profit = 100 * (price / position.average_price - 1)
            below = position.floor_pct is not None and profit < position.floor_pct
            if timers.trailing.check(c.trailing_enabled and below, now, c.trailing_seconds):
                reasons.append("profit_trailing")
        else:
            timers.high.since = timers.trailing.since = None
            timers.candidate = None
        bad_trend = (
            c.trend_enabled
            and snap.trend is not None
            and snap.imbalance_w is not None
            and snap.trend < c.trend_threshold
            and snap.imbalance_w < c.imbalance_exit
        )
        if timers.trend.check(bad_trend, now, c.trend_seconds):
            reasons.append("trend_imbalance")
        return reasons
