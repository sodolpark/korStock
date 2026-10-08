from __future__ import annotations

import bisect
import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from operator import itemgetter

from ..config import DataConfig, StrategyConfig
from ..market.session import TICKS
from ..models import Book, Side, Trade


@dataclass
class Second:
    start: int
    mid: float | None
    low: float | None
    high: float | None
    imbalance: float | None
    coverage: float


@dataclass
class Snapshot:
    at: float
    ready: bool = False
    valid_book: bool = False
    mid: float = 0
    bid: int = 0
    ask: int = 0
    tick: int = 1
    r60: float | None = None
    r10: float | None = None
    turnover60: float = 0
    turnover10: float = 0
    volume60: int = 0
    trades60: int = 0
    strength20: float | None = None
    unknown_fraction20: float = 1
    imbalance10: float | None = None
    imbalance_w: float | None = None
    trend: float | None = None
    sigma_pct: float | None = None
    window_target: int = 360
    window_effective: int = 0
    coverage: float = 0
    stable: bool = False
    breakout: int | None = None
    trade: Trade | None = None
    trade_fresh: bool = False
    reasons: list[str] = field(default_factory=list)


class RollingTrades:
    """Exact integer totals of trades received in (now - length, now]; the clock never moves back."""

    def __init__(self, length: int):
        self.length = length
        self.trades: deque[tuple[float, Trade]] = deque()
        self.clear()

    def clear(self):
        self.trades.clear()
        self.amount = self.quantity = 0
        self.sides = dict.fromkeys(Side, 0)

    def add(self, at: float, trade: Trade):
        self.trades.append((at, trade))
        self.amount += trade.price * trade.quantity
        self.quantity += trade.quantity
        self.sides[trade.side] += trade.quantity

    def expire(self, now: float):
        while self.trades and self.trades[0][0] <= now - self.length:
            _, trade = self.trades.popleft()
            self.amount -= trade.price * trade.quantity
            self.quantity -= trade.quantity
            self.sides[trade.side] -= trade.quantity


class FeatureEngine:
    """Causal receive-time aggregation. Every second integrates book duration, not tick count."""

    def __init__(self, data: DataConfig, strategy: StrategyConfig):
        self.data, self.strategy = data, strategy
        self.seconds: deque[Second] = deque(maxlen=720)
        self.windows = {length: RollingTrades(length) for length in (60, 20, 10)}
        self.book: Book | None = None
        self.book_history: deque[tuple[float, Book]] = deque()
        self.book_at = -math.inf
        self.last_trade: tuple[float, Trade] | None = None
        self.cursor: float | None = None
        self.start: float | None = None
        self.area_mid = self.area_i = self.duration = 0.0
        self.low = math.inf
        self.high = -math.inf
        self.window_target = strategy.fixed_window or 360
        self.last_window_eval = -math.inf
        self.suspended = False
        # Bumped whenever completed-second history changes; per-second statistics are cached per version.
        self.version = 0
        self.cache: dict[str, tuple] = {}

    def reset(self, now: float):
        self.seconds.clear()
        self.version += 1
        for window in self.windows.values():
            window.clear()
        self.book = None
        self.book_history.clear()
        self.book_at = -math.inf
        self.last_trade = None
        self.cursor = now
        self.start = now
        self.area_mid = self.area_i = self.duration = 0.0
        self.low, self.high = math.inf, -math.inf
        self.last_window_eval = -math.inf

    def advance(self, now: float):
        if self.cursor is None:
            self.cursor = self.start = now
        if now < self.cursor:
            raise ValueError("feature clock moved backwards")
        # A long outage never supplies historical prices, and does not require millions of empty bins.
        if now - self.cursor > 720:
            self.reset(now)
            return
        while self.cursor < now:
            boundary = math.floor(self.cursor) + 1
            end = min(now, boundary)
            available_end = min(end, self.book_at + self.data.book_fresh_seconds)
            if self.book and self.book.valid and not self.suspended and available_end > self.cursor:
                duration = available_end - self.cursor
                imbalance = self.book.imbalance
                if imbalance is not None:
                    self.duration += duration
                    self.area_mid += self.book.mid * duration
                    self.area_i += imbalance * duration
                    self.low = min(self.low, self.book.mid)
                    self.high = max(self.high, self.book.mid)
            if end == boundary:
                valid = self.duration >= self.data.coverage
                self.seconds.append(
                    Second(
                        boundary - 1,
                        self.area_mid / self.duration if valid else None,
                        self.low if valid else None,
                        self.high if valid else None,
                        self.area_i / self.duration if valid else None,
                        self.duration,
                    )
                )
                self.version += 1
                self.area_mid = self.area_i = self.duration = 0.0
                self.low, self.high = math.inf, -math.inf
            self.cursor = end
        for window in self.windows.values():
            window.expire(now)
        # Keep the last book known at now-60, the oldest return reference a non-decreasing clock needs.
        while len(self.book_history) > 1 and self.book_history[1][0] <= now - 60:
            self.book_history.popleft()

    def on_book(self, now: float, book: Book):
        self.advance(now)
        self.book, self.book_at = book, now
        self.book_history.append((now, book))

    def on_trade(self, now: float, trade: Trade):
        self.advance(now)
        # Do not deduplicate identical market trades: no authoritative execution id is provided.
        for window in self.windows.values():
            window.add(now, trade)
        self.last_trade = (now, trade)

    def cached(self, name: str, key, compute, *args):
        hit = self.cache.get(name)
        if hit is None or hit[0] != key:
            hit = self.cache[name] = (key, compute(*args))
        return hit[1]

    def recent(self, start: int) -> list[Second]:
        """Completed seconds starting at or after `start`, oldest first; history is time ordered."""
        recent = []
        for second in reversed(self.seconds):
            if second.start < start:
                break
            recent.append(second)
        recent.reverse()
        return recent

    def sigma(self) -> float | None:
        previous61 = list(self.seconds)[-61:]
        if len(previous61) == 61 and all(s.mid is not None for s in previous61):
            returns = [math.log(b.mid / a.mid) for a, b in zip(previous61, previous61[1:], strict=False)]
            return statistics.pstdev(returns) * 100
        return None

    def continuous(self) -> int:
        continuous = 0
        for second in reversed(self.seconds):
            if second.mid is None:
                break
            continuous += 1
        return continuous

    def trend(self, length: int) -> tuple[float, float]:
        window = list(self.seconds)[-length:]
        points = [(s.start / 60, math.log(s.mid)) for s in window if s.mid is not None]
        xbar = statistics.mean(x for x, _ in points)
        ybar = statistics.mean(y for _, y in points)
        denominator = sum((x - xbar) ** 2 for x, _ in points)
        trend = 10000 * sum((x - xbar) * (y - ybar) for x, y in points) / denominator
        imbalance = sum(s.imbalance * s.coverage for s in window) / sum(s.coverage for s in window)
        return trend, imbalance

    def stability(self, boundary5: int) -> tuple[float, float, float] | None:
        bins20 = [s for s in self.seconds if boundary5 - 20 <= s.start < boundary5]
        if len(bins20) == 20 and all(s.mid is not None for s in bins20):
            span = max(s.high for s in bins20) - min(s.low for s in bins20)
            return (
                span / bins20[-1].mid * 100,
                min(s.low for s in bins20[10:]),
                min(s.low for s in bins20[:10]),
            )
        return None

    def bucket_high(self, boundary5: int) -> int | None:
        """Highest trade price in the completed 5-second bucket ending at `boundary5`."""
        high = None
        for at, trade in reversed(self.windows[60].trades):
            if at < boundary5 - 5:
                break
            if at < boundary5 and (high is None or trade.price > high):
                high = trade.price
        return high

    def snapshot(self, now: float) -> Snapshot:
        self.advance(now)
        snap = Snapshot(at=now)
        snap.valid_book = bool(
            self.book
            and self.book.valid
            and not self.suspended
            and 0 <= now - self.book_at <= self.data.book_fresh_seconds
        )
        if not snap.valid_book:
            snap.reasons.append("stale_or_invalid_book")
            return snap
        book = self.book
        snap.mid, snap.bid, snap.ask = book.mid, book.bids[0].price, book.asks[0].price
        snap.tick = TICKS.size(snap.bid)
        recent60 = self.recent(math.floor(now) - 60)
        valid60 = [s for s in recent60 if s.mid is not None]
        snap.coverage = sum(s.coverage for s in recent60) / 60
        snap.ready = len(recent60) == 60 and snap.coverage >= self.data.coverage
        for length, key in [(10, "r10"), (60, "r60")]:
            # Use the last book actually known at t-W, never a later bucket average.
            index = bisect.bisect_right(self.book_history, now - length, key=itemgetter(0))
            reference = self.book_history[index - 1] if index else None
            if (
                reference
                and reference[1].valid
                and now - length - reference[0] <= self.data.book_fresh_seconds
            ):
                setattr(snap, key, 100 * (snap.mid / reference[1].mid - 1))
        recent10 = self.recent(math.floor(now) - 10)
        if len(recent10) == 10 and sum(s.coverage for s in recent10) >= 10 * self.data.coverage:
            valid = [s for s in recent10 if s.imbalance is not None]
            snap.imbalance10 = (
                sum(s.imbalance * s.coverage for s in valid) / sum(s.coverage for s in valid)
                if valid
                else None
            )
        boundary5 = int(now // 5 * 5)
        last60, last20 = self.windows[60], self.windows[20]
        snap.turnover60, snap.volume60, snap.trades60 = last60.amount, last60.quantity, len(last60.trades)
        snap.turnover10 = self.windows[10].amount
        buy, sell, unknown = (last20.sides[side] for side in (Side.BUY, Side.SELL, Side.UNKNOWN))
        total = buy + sell + unknown
        snap.unknown_fraction20 = unknown / total if total else 1
        snap.strength20 = 100 * buy / sell if sell else None
        snap.sigma_pct = self.cached("sigma", self.version, self.sigma)
        if now - self.last_window_eval >= 10:
            if snap.sigma_pct is not None:
                target = max(120, min(360, 360 / (1 + 10 * snap.sigma_pct)))
                self.window_target = self.strategy.fixed_window or int(target // 10 * 10)
            self.last_window_eval = now
        snap.window_target = self.window_target
        continuous = self.cached("continuous", self.version, self.continuous)
        snap.window_effective = min(self.window_target, continuous // 10 * 10)
        if snap.window_effective >= 120:
            snap.trend, snap.imbalance_w = self.cached(
                "trend", (self.version, snap.window_effective), self.trend, snap.window_effective
            )
        stability = self.cached("stability", (self.version, boundary5), self.stability, boundary5)
        if stability:
            range_pct, late_low, early_low = stability
            snap.stable = range_pct <= self.strategy.stable_range_pct and late_low >= early_low - snap.tick
        # Later trades are received at or after the boundary, so a bucket's high is final once observed.
        previous_high = self.cached("bucket_high", (self.version, boundary5), self.bucket_high, boundary5)
        if previous_high is not None and TICKS.legal(previous_high):
            snap.breakout = TICKS.step(previous_high)
        if self.last_trade:
            at, trade = self.last_trade
            snap.trade = trade
            # Cross-check isolated prints, without suppressing bid-based stop loss.
            snap.trade_fresh = (
                now - at <= self.data.book_fresh_seconds
                and snap.bid - snap.tick * 2 <= trade.price <= snap.ask + TICKS.size(snap.ask) * 2
            )
        if len(valid60) < 54:
            snap.ready = False
        return snap
