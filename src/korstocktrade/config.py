from __future__ import annotations

import hashlib
import json
import math
import tomllib
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .models import Model


class Costs(Model):
    buy_fee: float = Field(ge=0, lt=0.1)
    sell_fee: float = Field(ge=0, lt=0.1)
    sell_tax: float = Field(ge=0, lt=0.1)
    stress_slippage: float = Field(default=0.001, ge=0, lt=0.1)
    verified: bool = False
    basis: str

    @property
    def round_trip(self) -> float:
        return self.buy_fee + self.sell_fee + self.sell_tax + self.stress_slippage


class StrategyConfig(Model):
    min_turnover_60: float = Field(default=500_000_000, gt=0)
    min_trades_60: int = Field(default=100, gt=0)
    spread_ticks: int = Field(default=2, gt=0)
    spread_pct: float = Field(default=0.20, gt=0)
    r60_min_pct: float = -0.50
    r10_min_pct: float = -0.20
    imbalance_stage1: float = -0.70
    imbalance_stage2: float = -0.50
    stable_range_pct: float = Field(default=0.60, gt=0)
    strength_min: float = Field(default=120, gt=0)
    turnover_acceleration: float = Field(default=1.2, gt=0)
    candidate_ttl: float = Field(default=180, gt=0)
    stage2_ttl: float = Field(default=30, gt=0)
    cooldown: float = Field(default=20, ge=0)
    max_entries: int = Field(default=3, gt=0)
    stop_streak: int = Field(default=2, gt=0)
    stop_loss_pct: float = Field(default=1.5, gt=0)
    trailing_enabled: bool = True
    trend_enabled: bool = True
    trailing_activation_pct: float = Field(default=2, gt=0)
    profit_protection: float = Field(default=0.8, gt=0, le=1)
    high_confirm_seconds: float = Field(default=10, gt=0)
    high_tolerance: float = Field(default=0.001, ge=0, lt=1)
    trailing_seconds: float = Field(default=10, gt=0)
    trend_seconds: float = Field(default=21, gt=0)
    trend_threshold: float = 0
    imbalance_exit: float = -0.70
    fixed_window: int | None = Field(default=None, ge=120, le=360, multiple_of=10)


class RiskConfig(Model):
    equity: float = Field(default=10_000_000, gt=0)
    risk_per_trade: float = Field(default=0.002, gt=0, le=0.002)
    per_name: float = Field(default=0.20, gt=0, le=0.20)
    portfolio: float = Field(default=0.50, gt=0, le=0.50)
    participation: float = Field(default=0.01, gt=0, le=0.01)
    daily_loss: float = Field(default=0.01, gt=0, le=0.01)
    max_positions: int = Field(default=3, ge=1, le=3)


class DataConfig(Model):
    book_fresh_seconds: float = Field(default=2, gt=0, le=2)
    alarm_seconds: float = Field(default=5, gt=2)
    coverage: float = Field(default=0.90, ge=0.90, le=1)
    max_symbols: int = Field(default=30, ge=1, le=50)
    retention_seconds: float = Field(default=360, ge=360)
    ranking_interval: float = Field(default=30, ge=30)
    queue_size: int = Field(default=10000, ge=1)
    max_processing_lag: float = Field(default=1, gt=0)


class BrokerConfig(Model):
    environment: Literal["mock", "live"] = "mock"
    account_alias: str = Field(default="research", pattern=r"^[A-Za-z0-9_-]+$")
    query_per_second: float = Field(default=5, gt=0, le=5)
    order_per_second: float = Field(default=5, gt=0, le=5)
    mock_per_tr_second: float = Field(default=1, gt=0, le=1)
    timeout: float = Field(default=10, gt=0)


class SimulationConfig(Model):
    latency_seconds: float = Field(default=0.25, ge=0)
    cancel_latency_seconds: float = Field(default=0.25, ge=0)
    depth_fraction: float = Field(default=0.25, gt=0, le=1)
    adverse_ticks: int = Field(default=0, ge=0, le=10)


class Settings(Model):
    profile: Literal["research_v1", "live_candidate"] = "research_v1"
    mode: Literal["replay", "collect", "shadow", "mock", "live"] = "replay"
    timezone: Literal["Asia/Seoul"] = "Asia/Seoul"
    market: Literal["KRX"] = "KRX"
    live_enabled: bool = False
    costs: Costs
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    broker: BrokerConfig = Field(default_factory=BrokerConfig)
    simulation: SimulationConfig = Field(default_factory=SimulationConfig)

    @model_validator(mode="after")
    def modes(self):
        if self.mode == "mock" and self.broker.environment != "mock":
            raise ValueError("mock orders require the mock environment")
        if self.mode == "live":
            if not (
                self.live_enabled
                and self.profile == "live_candidate"
                and self.costs.verified
                and self.broker.environment == "live"
            ):
                raise ValueError("live requires a validated live_candidate and verified costs")
            if self.risk.max_positions != 1:
                raise ValueError("initial live validation is limited to one symbol")
        return self

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        with Path(path).open("rb") as stream:
            return cls.model_validate(tomllib.load(stream))


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for file in sorted(root.rglob("*.py")):
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


def verify_live_gate(settings: Settings, path: Path, artifacts_root: Path) -> None:
    """Validate frozen evidence and artifact digests before constructing an order adapter."""
    gate = json.loads(path.read_text(encoding="utf-8"))
    required = {"operational_pass", "performance_pass", "mock_contract_pass", "stress_pass"}
    if any(gate.get(key) is not True for key in required):
        raise ValueError("live qualification checks have not all passed")
    dates = {date.fromisoformat(value) for value in gate.get("shadow_dates", [])}
    if len(dates) < 20:
        raise ValueError("at least 20 distinct shadow trading days are required")
    if gate.get("settings_hash") != settings.fingerprint or gate.get("code_hash") != code_fingerprint():
        raise ValueError("live evidence does not match this configuration/code")
    lower, limit, measured = (gate.get(key) for key in ("bootstrap_lower_95", "mdd_limit", "measured_mdd"))
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
        for value in (lower, limit, measured)
    ):
        raise ValueError("finite numeric performance evidence is required")
    if lower <= 0 or not 0 < limit <= 1 or measured < 0:
        raise ValueError("performance evidence is incomplete")
    if measured > limit:
        raise ValueError("drawdown exceeds the predeclared limit")
    artifacts = gate.get("artifacts", {})
    if not artifacts:
        raise ValueError("qualification artifacts are required")
    root = artifacts_root.resolve()
    for name, expected in artifacts.items():
        file = (root / name).resolve()
        if not file.is_relative_to(root) or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError(f"artifact mismatch: {name}")
