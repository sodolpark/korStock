"""Period backtest: replay every recorded trading day in a date range and aggregate the results.

Kiwoom does not serve historical 10-level books, so only days captured by `run --mode collect/shadow`
(or imported in the same event schema) can be replayed. Missing days are reported, never synthesized.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import Settings, code_fingerprint
from ..market.session import KST, Session
from ..market.universe import load_instruments
from ..models import Event, Instrument
from .evaluate import (
    bootstrap_daily_lower,
    date_split,
    maximum_drawdown,
    summarize_groups,
    write_report,
)
from .runner import replay

STRESS_SCENARIOS = {
    "baseline": {},
    "double_cost": {"costs": 2},
    "adverse_1tick": {"adverse_ticks": 1},
    "adverse_2ticks": {"adverse_ticks": 2},
    "latency_1s": {"latency_seconds": 1.0},
    "risk_and_time": {"trailing_enabled": False, "trend_enabled": False},
    "with_trailing": {"trend_enabled": False},
    "fixed_360": {"fixed_window": 360},
}


class MissingInput(ValueError):
    pass


def scenario_settings(settings: Settings, changes: dict) -> Settings:
    body = settings.model_dump()
    body["mode"] = "replay"
    for key, value in changes.items():
        if key == "costs":
            for cost in ("buy_fee", "sell_fee", "sell_tax"):
                body["costs"][cost] *= value
        elif key in body["simulation"]:
            body["simulation"][key] = value
        else:
            body["strategy"][key] = value
    return Settings.model_validate(body)


def backtest_period(
    today: date, start: date | None = None, end: date | None = None, days: int = 365
) -> tuple[date, date]:
    """Default: the `days` calendar days ending yesterday, since today's log may still be written."""
    if days < 1:
        raise ValueError("backtest period must contain at least one day")
    end = end or today - timedelta(days=1)
    start = start or end - timedelta(days=days - 1)
    if start > end:
        raise ValueError("backtest start is after its end")
    return start, end


def recorded_runs(namespace: Path, day: date, modes) -> list[Path]:
    """Non-empty run logs of one date, ordered by their first receive time."""
    runs = []
    for mode in modes:
        for path in (namespace / day.isoformat() / mode).glob("events-*.jsonl"):
            with path.open(encoding="utf-8") as stream:
                first = stream.readline()
            if first.strip():
                runs.append((Event.model_validate_json(first).received_at, path))
    return [path for _, path in sorted(runs)]


def load_dated_instruments(path: Path) -> dict[date, dict[str, Instrument]]:
    """Instrument rows apply to their own verified_date; one row per symbol and date."""
    dated: dict[date, dict[str, Instrument]] = defaultdict(dict)
    for row in json.loads(path.read_text(encoding="utf-8")):
        instrument = Instrument.model_validate(row)
        if instrument.symbol in dated[instrument.verified_date]:
            raise ValueError(f"duplicate instrument metadata on {instrument.verified_date}")
        dated[instrument.verified_date][instrument.symbol] = instrument
    return dict(dated)


def day_inputs(
    day: date, runs: list[Path], sessions: dict, instruments: dict[date, dict[str, Instrument]]
) -> tuple[Session, dict[str, Instrument]]:
    """Explicit dated inputs win; otherwise use the inputs `run` recorded beside each log."""
    recorded_sessions, recorded_instruments = [], {}
    for path in runs:
        run_id = path.stem.removeprefix("events-")
        session_file = path.with_name(f"session-{run_id}.json")
        if session_file.is_file():
            recorded_sessions.append(Session.load(session_file, day))
        instruments_file = path.with_name(f"instruments-{run_id}.json")
        if instruments_file.is_file():
            recorded_instruments.update(load_instruments(instruments_file))
    if day.isoformat() in sessions:
        session = Session.model_validate(sessions[day.isoformat()])
    elif recorded_sessions:
        session = recorded_sessions[0]
        if any(other != session for other in recorded_sessions):
            raise MissingInput("conflicting_recorded_sessions")
    else:
        raise MissingInput("missing_session")
    if session.trading_date != day:
        raise MissingInput("session_date_mismatch")
    if session.closed:
        raise MissingInput("closed_session")
    chosen = instruments.get(day) or recorded_instruments
    if not chosen:
        raise MissingInput("missing_instruments")
    return session, chosen


def turning_points(values: list[float]) -> list[float]:
    """Drop repeated and monotone interior points; the maximum drawdown is unchanged."""
    points: list[float] = []
    for value in values:
        if points and value == points[-1]:
            continue
        if len(points) >= 2 and (points[-2] < points[-1] < value or points[-2] > points[-1] > value):
            points[-1] = value
        else:
            points.append(value)
    return points


def day_summary(result: dict) -> dict:
    return {
        "net_realized": result["net_realized"],
        "orders": result["orders"],
        "requested_quantity": result["requested_quantity"],
        "filled_quantity": result["filled_quantity"],
        "fees_and_taxes": result["fees_and_taxes"],
        "trades": result["trades"],
        "equity": turning_points([value for _, value in result["equity_curve"]]),
        "events": result["quality"]["events"],
        "invalid": result["quality"]["invalid"],
        "gaps": result["quality"]["gaps"],
        "closing_handoff_required": result["closing_handoff_required"],
        "account_state": result["account_state"],
        "data_hash": result["data_hash"],
        "signal_hash": result["signal_hash"],
    }


def replay_day(task: tuple) -> dict:
    """Process-pool worker. The baseline's full report is written beside the period summary."""
    day, runs, settings, session, instruments, scenarios, output = task
    try:
        summaries = {}
        for name in scenarios:
            result = replay(runs, scenario_settings(settings, STRESS_SCENARIOS[name]), session, instruments)
            if name == "baseline":
                write_report(output / "days" / f"{day}.json", result)
            summaries[name] = day_summary(result)
        return {"date": day.isoformat(), "runs": [str(path) for path in runs], "scenarios": summaries}
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        return {"date": day.isoformat(), "error": f"{type(exc).__name__}: {exc}"}


def summarize_period(days: list[dict], equity: float, detail: bool = True) -> dict:
    """Each day is an independent account day starting at `equity`; daily nets add without compounding."""
    trades = [trade for day in days for trade in day["trades"]]
    pnls = [trade["net"] for trade in trades]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    nets = [day["net_realized"] for day in days]
    curve, offset, daily = [equity], 0.0, []
    for day in days:
        # Intraday marks are relative to the same daily start; shift them by prior days' results.
        curve.extend(value + offset for value in day["equity"])
        offset += day["net_realized"]
        curve.append(equity + offset)
        daily.append(
            {
                "date": day["date"],
                "net": day["net_realized"],
                "trades": len(day["trades"]),
                "equity": curve[-1],
            }
        )
    requested = sum(day["requested_quantity"] for day in days)
    result = {
        "first_date": days[0]["date"] if days else None,
        "last_date": days[-1]["date"] if days else None,
        "days": len(days),
        "round_trips": len(trades),
        "net_realized": sum(nets),
        "return_on_equity": sum(nets) / equity,
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "expectancy": statistics.mean(pnls) if pnls else None,
        "average_win": statistics.mean(wins) if wins else None,
        "average_loss": statistics.mean(losses) if losses else None,
        "profit_factor": sum(wins) / -sum(losses) if losses else None,
        "fees_and_taxes": sum(day["fees_and_taxes"] for day in days),
        "orders": sum(day["orders"] for day in days),
        "fill_rate": sum(day["filled_quantity"] for day in days) / requested if requested else None,
        "max_drawdown": maximum_drawdown(curve),
        "worst_day": min(nets, default=None),
        "best_day": max(nets, default=None),
        "positive_day_rate": sum(net > 0 for net in nets) / len(nets) if nets else None,
        "bootstrap_lower_95": bootstrap_daily_lower(
            {day["date"]: [trade["net"] for trade in day["trades"]] for day in days if day["trades"]}
        ),
        "closing_handoff_days": [day["date"] for day in days if day["closing_handoff_required"]],
        "halted_days": [day["date"] for day in days if day["account_state"] != "RUNNING"],
    }
    if not detail:
        return result
    groups = {name: defaultdict(list) for name in ("by_month", "by_symbol", "by_hour_kst", "by_exit_reason")}
    for trade in trades:
        local = datetime.fromtimestamp(trade["entry_at"], KST)
        groups["by_month"][f"{local:%Y-%m}"].append(trade["net"])
        groups["by_symbol"][trade["symbol"]].append(trade["net"])
        groups["by_hour_kst"][str(local.hour)].append(trade["net"])
        groups["by_exit_reason"][trade["reasons"][0] if trade["reasons"] else "unknown"].append(trade["net"])
    result |= {name: summarize_groups(dict(sorted(group.items()))) for name, group in groups.items()}
    result["quality"] = {
        "events": sum(day["events"] for day in days),
        "invalid": sum(day["invalid"] for day in days),
        "gaps": sum(day["gaps"] for day in days),
    }
    if len(days) >= 5:
        by_date = {day["date"]: day for day in days}
        result["split"] = {
            name: summarize_period([by_date[value] for value in dates], equity, detail=False)
            for name, dates in date_split(list(by_date)).items()
        }
    result["daily"] = daily
    return result


def backtest(
    settings: Settings,
    root: Path,
    start: date,
    end: date,
    output: Path,
    *,
    modes=("collect", "shadow"),
    sessions: dict | None = None,
    instruments: dict[date, dict[str, Instrument]] | None = None,
    stress: bool = False,
    workers: int = 1,
    progress: Callable[[str], None] | None = None,
) -> dict:
    if start > end:
        raise ValueError("backtest start is after its end")
    if workers < 1:
        raise ValueError("at least one worker is required")
    namespace = root / settings.broker.environment / settings.broker.account_alias
    scenarios = list(STRESS_SCENARIOS) if stress else ["baseline"]
    tasks, missing, skipped = [], [], {}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        try:
            runs = recorded_runs(namespace, day, modes)
            if not runs:
                if day.weekday() < 5:
                    missing.append(day.isoformat())
                continue
            session, chosen = day_inputs(day, runs, sessions or {}, instruments or {})
        except MissingInput as exc:
            skipped[day.isoformat()] = str(exc)
            continue
        except (ValueError, OSError, KeyError) as exc:
            skipped[day.isoformat()] = f"{type(exc).__name__}: {exc}"
            continue
        tasks.append((day, runs, settings, session, chosen, scenarios, output))

    results = []

    def collect(result: dict):
        results.append(result)
        if progress:
            baseline = result.get("scenarios", {}).get("baseline")
            detail = (
                f"trades={len(baseline['trades'])} net={baseline['net_realized']:,.0f}"
                if baseline
                else result["error"]
            )
            progress(f"[{len(results)}/{len(tasks)}] {result['date']} {detail}")

    if workers > 1 and len(tasks) > 1:
        # Days are independent account days, so they replay in parallel without changing results.
        with ProcessPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
            for future in as_completed([pool.submit(replay_day, task) for task in tasks]):
                collect(future.result())
    else:
        for task in tasks:
            collect(replay_day(task))

    failed = {r["date"]: r["error"] for r in sorted(results, key=lambda r: r["date"]) if "error" in r}
    replayed = sorted((r for r in results if "error" not in r), key=lambda r: r["date"])
    equity = settings.risk.equity
    data = [[r["date"], r["scenarios"]["baseline"]["data_hash"]] for r in replayed]
    summary = {
        "schema_version": 1,
        "kind": "period_backtest",
        "performance_label": "simulated estimate",
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "calendar_days": (end - start).days + 1,
        },
        "source": {"namespace": str(namespace), "modes": list(modes)},
        "equity_basis": "non-compounding: every trading day starts at risk.equity",
        "start_equity": equity,
        "cost_basis": settings.costs.basis,
        "costs_verified": settings.costs.verified,
        "code_hash": code_fingerprint(),
        "data_hash": hashlib.sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest(),
        "coverage": {
            "replayed_days": len(replayed),
            "missing_weekdays": missing,
            "skipped": skipped,
            "failed": failed,
        },
        "scenarios": {
            name: {"settings_hash": scenario_settings(settings, STRESS_SCENARIOS[name]).fingerprint}
            | summarize_period(
                [{"date": r["date"]} | r["scenarios"][name] for r in replayed], equity, detail=True
            )
            for name in scenarios
        },
        "days": {r["date"]: {"runs": r["runs"], "report": f"days/{r['date']}.json"} for r in replayed},
        "qualification": "UNVALIDATED",
        "limitations": [
            "Only recorded tick/10-level logs are replayed; Kiwoom provides no historical 10-level books.",
            "Weekdays without logs include exchange holidays; coverage must be judged against the KRX calendar.",
            "Positions are not carried overnight; closing handoff days end with unrealized inventory.",
            "Passive queue position and closing-auction fills are not inferred.",
            "Qualification needs predeclared MDD limits, a sealed holdout and verified costs.",
        ],
    }
    write_report(output / "summary.json", summary)
    return summary
