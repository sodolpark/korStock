from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import model_validator

from ..models import Model

KST = ZoneInfo("Asia/Seoul")


class Session(Model):
    trading_date: date
    closed: bool = False
    open: time = time(9)
    entry_start: time = time(9, 2)
    entry_end: time = time(14, 50)
    exit_start: time = time(15, 19)
    auction_start: time = time(15, 20)
    close: time = time(15, 30)
    verified: bool = False
    source: str = "synthetic/replay schedule"

    @model_validator(mode="after")
    def ordered(self):
        if (
            not self.open
            <= self.entry_start
            < self.entry_end
            <= self.exit_start
            < self.auction_start
            < self.close
        ):
            raise ValueError("invalid session boundary order")
        return self

    def local(self, timestamp: float) -> datetime:
        return datetime.fromtimestamp(timestamp, KST)

    def phase(self, timestamp: float) -> str:
        now = self.local(timestamp)
        if self.closed or now.date() != self.trading_date:
            return "CLOSED"
        if self.open <= now.time() < self.auction_start:
            return "CONTINUOUS"
        if self.auction_start <= now.time() < self.close:
            return "AUCTION"
        return "CLOSED"

    def entry_allowed(self, timestamp: float) -> bool:
        return (
            self.phase(timestamp) == "CONTINUOUS"
            and self.entry_start <= self.local(timestamp).time() < self.entry_end
        )

    def exit_due(self, timestamp: float) -> bool:
        now = self.local(timestamp)
        return now.date() > self.trading_date or (
            now.date() == self.trading_date and now.time() >= self.exit_start
        )

    @classmethod
    def load(cls, path: Path, day: date, online: bool = False) -> Session:
        content = json.loads(path.read_text(encoding="utf-8"))
        session = cls.model_validate(content[day.isoformat()])
        if session.trading_date != day or (online and not session.verified):
            raise ValueError("explicit verified schedule for this trading day is required")
        return session


class TickTable(Model):
    """KRX ordinary-share ladder; versioned separately from daily price limits."""

    version: str = "KRX-common-2023-01-25"
    bands: list[tuple[int, int]] = [
        (2000, 1),
        (5000, 5),
        (20000, 10),
        (50000, 50),
        (200000, 100),
        (500000, 500),
        (2**63 - 1, 1000),
    ]

    def size(self, price: float) -> int:
        if price <= 0:
            raise ValueError("price must be positive")
        return next(tick for upper, tick in self.bands if price < upper)

    def legal(self, price: int) -> bool:
        return price > 0 and price % self.size(price) == 0

    def step(self, price: int, count: int = 1) -> int:
        if not self.legal(price):
            raise ValueError("off-grid price")
        for _ in range(abs(count)):
            price += self.size(price) if count > 0 else -self.size(price - 1)
        return price


TICKS = TickTable()
