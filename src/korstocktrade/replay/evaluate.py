from __future__ import annotations

import hashlib
import json
import random
import statistics
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from ..market.session import KST


def maximum_drawdown(equity: list[float]) -> float:
    peak = 0.0
    drawdown = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, (peak - value) / peak)
    return drawdown


def date_split(dates: list[str]) -> dict[str, list[str]]:
    dates = sorted({date.fromisoformat(value).isoformat() for value in dates})
    if len(dates) < 5:
        raise ValueError("at least five distinct trading dates are required for chronological splits")
    first, second = int(len(dates) * 0.6), int(len(dates) * 0.8)
    return {"development": dates[:first], "validation": dates[first:second], "holdout": dates[second:]}


def purge_cross_boundary(records: list[dict], start: float, end: float, lookback: int = 360) -> list[dict]:
    return [r for r in records if r["entry_at"] - lookback >= start and r["exit_at"] < end]


def bootstrap_daily_lower(
    daily: dict[str, list[float]], iterations: int = 2000, seed: int = 47
) -> float | None:
    """Resample whole date blocks; return lower endpoint of the two-sided 95% interval."""
    if len(daily) < 20 or any(not values for values in daily.values()):
        return None
    blocks = list(daily.values())
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [value for block in rng.choices(blocks, k=len(blocks)) for value in block]
        means.append(statistics.mean(sample))
    return sorted(means)[int(iterations * 0.025)]


def summarize_groups(groups: dict[str, list[float]]) -> dict:
    return {
        key: {"trades": len(values), "net": sum(values), "average": statistics.mean(values)}
        for key, values in groups.items()
    }


def signal_digest(signals: list[dict]) -> str:
    # Execution mode and environment metadata do not alter the strategy comparison.
    payload = [
        {k: row[k] for k in ("at", "sequence", "action", "symbol", "reasons", "features")} for row in signals
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def report(core, *, data_hash: str, source: str) -> dict:
    ledger = core.ledger
    trades = ledger.audits("round_trip")
    pnls = [row["net"] for row in trades]
    orders = ledger.orders()
    daily = defaultdict(list)
    symbols = defaultdict(list)
    hours = defaultdict(list)
    for row in trades:
        local = datetime.fromtimestamp(row["entry_at"], KST)
        daily[local.date().isoformat()].append(row["net"])
        symbols[row["symbol"]].append(row["net"])
        hours[str(local.hour)].append(row["net"])
    equity = [ledger.get("start_equity"), *[v for _, v in core.equity_curve]]
    fills = [json.loads(row[0]) for row in ledger.db.execute("SELECT body FROM fills ORDER BY rowid")]
    remaining = {p.symbol: p.quantity for p in ledger.positions() if p.quantity}
    return {
        "schema_version": 1,
        "mode": core.settings.mode,
        "performance_label": "simulated estimate" if core.simulator else "broker observations",
        "source": source,
        "cost_basis": core.settings.costs.basis,
        "costs_verified": core.settings.costs.verified,
        "data_hash": data_hash,
        "settings_hash": core.settings.fingerprint,
        "code_hash": ledger.get("code_hash"),
        "signal_hash": signal_digest(core.signals),
        "quality": {
            "events": core.event_count,
            "invalid": core.invalid_count,
            "gaps": core.gaps,
            "exchange_latency_precision": "exchange timestamps have second precision; no millisecond estimate",
            "market_completeness_proven": False,
        },
        "orders": len(orders),
        "requested_quantity": sum(o.quantity for o in orders),
        "filled_quantity": sum(o.filled for o in orders),
        "fill_rate": sum(o.filled for o in orders) / sum(o.quantity for o in orders) if orders else None,
        "round_trips": len(trades),
        "net_realized": ledger.get("realized", 0),
        "win_rate": sum(p > 0 for p in pnls) / len(pnls) if pnls else None,
        "expectancy": statistics.mean(pnls) if pnls else None,
        "max_drawdown": maximum_drawdown(equity),
        "worst_day": min((sum(v) for v in daily.values()), default=None),
        "bootstrap_lower_95": bootstrap_daily_lower(daily),
        "qualification": "UNVALIDATED",
        "mfe_pct": [r["mfe_pct"] for r in trades],
        "mae_pct": [r["mae_pct"] for r in trades],
        "fees_and_taxes": sum(f["fee"] for f in fills),
        "slippage": ledger.audits("slippage"),
        "reentry_trades": sum(r["reentry"] for r in trades),
        "by_symbol": summarize_groups(symbols),
        "by_hour_kst": summarize_groups(hours),
        "by_day": summarize_groups(daily),
        "by_regime": "not available; regime labels required",
        "theme_exposure": "not evaluated; theme metadata absent",
        "remaining_positions": remaining,
        "open_orders": [o.model_dump(mode="json") for o in orders if o.active],
        "closing_handoff_required": bool(remaining or any(o.active for o in orders)),
        "account_state": ledger.get("account_state"),
        "signals": core.signals,
        "trades": trades,
        "equity_curve": core.equity_curve,
        "limitations": [
            "Synthetic data validates behavior, not profitability.",
            "Passive queue position and closing-auction fills are not inferred.",
            "Actual account fees/taxes and dated instruments/session metadata must be supplied.",
        ],
    }


def write_report(path: Path, result: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
