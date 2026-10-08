from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


class Level(Model):
    price: int = Field(ge=0)
    quantity: int = Field(ge=0)


class Book(Model):
    """Received quotes are never mutated, so derived values are computed once per book."""

    bids: list[Level] = Field(min_length=10, max_length=10)
    asks: list[Level] = Field(min_length=10, max_length=10)

    @cached_property
    def valid(self) -> bool:
        bids = [x.price for x in self.bids if x.price > 0]
        asks = [x.price for x in self.asks if x.price > 0]
        return bool(
            bids
            and asks
            and self.bids[0].quantity > 0
            and self.asks[0].quantity > 0
            and bids[0] < asks[0]
            and all(a > b for a, b in zip(bids, bids[1:], strict=False))
            and all(a < b for a, b in zip(asks, asks[1:], strict=False))
        )

    @property
    def mid(self) -> float:
        return (self.bids[0].price + self.asks[0].price) / 2

    @cached_property
    def imbalance(self) -> float | None:
        weights = (1, 0.8, 0.6, 0.4, 0.2)
        bid = sum(w * x.quantity for w, x in zip(weights, self.bids, strict=False))
        ask = sum(w * x.quantity for w, x in zip(weights, self.asks, strict=False))
        return (bid - ask) / (bid + ask) if self.valid and bid + ask else None


class Trade(Model):
    price: int = Field(gt=0)
    quantity: int = Field(gt=0)
    side: Side
    cumulative_quantity: int | None = Field(default=None, ge=0)
    api_strength: float | None = None


class Event(Model):
    schema_version: Literal[1] = 1
    run_id: str
    sequence: int = Field(gt=0)
    generation: int = Field(ge=0)
    trading_date: date
    received_at: datetime
    monotonic_ns: int = Field(ge=0)
    kind: Literal[
        "trade",
        "book",
        "market",
        "vi",
        "order",
        "balance",
        "universe",
        "timer",
        "connection",
        "quality",
        "raw",
    ]
    symbol: str = ""
    market: Literal["KRX"] = "KRX"
    api: str = ""
    exchange_time: str | None = None
    trade: Trade | None = None
    book: Book | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    valid: bool = True
    issues: list[str] = Field(default_factory=list)

    @field_validator("received_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("received_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def payload(self):
        if self.valid and (
            (self.kind == "trade" and self.trade is None) or (self.kind == "book" and self.book is None)
        ):
            raise ValueError("missing event payload")
        return self


class Instrument(Model):
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = ""
    board: Literal["KOSPI", "KOSDAQ"]
    product: Literal["COMMON", "PREFERRED", "ETF", "ETN", "OTHER"]
    verified_date: date
    listing_date: date
    lower_limit: int = Field(gt=0)
    upper_limit: int = Field(gt=0)
    normal: bool = False
    halted: bool = False
    single_price: bool = False
    overheated: bool = False

    def eligible(self, day: date) -> bool:
        return (
            self.verified_date == day
            and self.listing_date < day
            and self.product == "COMMON"
            and self.normal
            and not (self.halted or self.single_price or self.overheated)
            and self.lower_limit < self.upper_limit
        )


class OrderState(StrEnum):
    CREATED = "CREATED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


TERMINAL = {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED}


class Order(Model):
    intent_id: str
    trading_date: date
    account: str
    symbol: str
    side: Literal[Side.BUY, Side.SELL]
    quantity: int = Field(gt=0)
    limit_price: int = Field(gt=0)
    created_at: float
    state: OrderState = OrderState.CREATED
    broker_id: str = ""
    original_id: str = ""
    filled: int = Field(default=0, ge=0)
    average_fill: float = Field(default=0, ge=0)
    acknowledged_at: float | None = None
    cancel_requested_at: float | None = None
    reasons: list[str] = Field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled

    @property
    def active(self) -> bool:
        return self.state not in TERMINAL


class Fill(Model):
    trading_date: date
    account: str
    broker_id: str
    execution_id: str
    symbol: str
    side: Literal[Side.BUY, Side.SELL]
    quantity: int = Field(gt=0)
    price: int = Field(gt=0)
    at: float
    fee: float = Field(default=0, ge=0)

    @property
    def key(self) -> str:
        if not self.execution_id or not self.broker_id:
            raise ValueError("fill identity missing; reconciliation required")
        return f"{self.trading_date}/{self.account}/{self.broker_id}/{self.execution_id}"


class Position(Model):
    symbol: str
    quantity: int = Field(default=0, ge=0)
    average_price: float = Field(default=0, ge=0)
    entry_fees: float = Field(default=0, ge=0)
    realized: float = 0
    cycle_realized: float = 0
    opened_at: float | None = None
    raw_high: float = 0
    confirmed_high: float = 0
    floor_pct: float | None = None
    exits: list[str] = Field(default_factory=list)
    entries: int = 0
    consecutive_stops: int = 0
    flat_since: float | None = None
    mfe_pct: float = 0
    mae_pct: float = 0
