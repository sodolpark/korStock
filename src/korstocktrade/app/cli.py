from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from ..config import Settings, code_fingerprint, verify_live_gate
from ..execution.orders import OrderManager
from ..execution.recovery import recover
from ..market.session import KST, Session
from ..market.universe import load_instruments
from ..replay.backtest import (
    STRESS_SCENARIOS,
    backtest,
    backtest_period,
    load_dated_instruments,
    scenario_settings,
)
from ..replay.demo import demo_dataset
from ..replay.evaluate import date_split, write_report
from ..replay.runner import replay
from ..storage.events import parquet_summary, to_parquet
from ..storage.ledger import Ledger
from ..storage.lock import AccountLock
from .probe import probe
from .service import run_online


def parser():
    root = argparse.ArgumentParser(description="KRX/Kiwoom capture, causal replay and execution")
    root.add_argument("--config", type=Path, default=Path("configs/research.toml"))
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate configuration without network access")
    demo = commands.add_parser("demo", help="generate and replay labeled synthetic data")
    demo.add_argument("--output", type=Path, default=Path("reports/runs/demo"))
    for command in ("replay", "run", "stress"):
        sub = commands.add_parser(command)
        sub.add_argument("--session", type=Path, required=True)
        sub.add_argument("--instruments", type=Path, required=True)
        sub.add_argument("--date", type=date.fromisoformat, required=True)
        if command in {"replay", "stress"}:
            sub.add_argument("events", type=Path)
            sub.add_argument("--output", type=Path, default=Path(f"reports/runs/{command}.json"))
        else:
            sub.add_argument("--mode", choices=["collect", "shadow", "mock", "live"], required=True)
            sub.add_argument("--root", type=Path, default=Path("data"))
            sub.add_argument("--duration", type=float)
            sub.add_argument("--ranking", action="store_true")
            sub.add_argument("--qualification", type=Path)
    period = commands.add_parser(
        "backtest", help="replay every recorded trading day in a period (default: last 365 days)"
    )
    period.add_argument("--root", type=Path, default=Path("data"))
    period.add_argument(
        "--modes", nargs="+", choices=["collect", "shadow", "mock", "live"], default=["collect", "shadow"]
    )
    period.add_argument("--start", type=date.fromisoformat)
    period.add_argument("--end", type=date.fromisoformat, help="default: yesterday (KST)")
    period.add_argument("--days", type=int, default=365, help="period length when --start is omitted")
    period.add_argument("--session", type=Path, help="dated sessions overriding those recorded by run")
    period.add_argument("--instruments", type=Path, help="instrument rows applied by their verified_date")
    period.add_argument("--stress", action="store_true", help="also evaluate every stress scenario")
    period.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    period.add_argument("--output", type=Path)
    convert = commands.add_parser("parquet")
    convert.add_argument("events", type=Path)
    convert.add_argument("output", type=Path)
    status = commands.add_parser("status")
    status.add_argument("path", type=Path)
    backup = commands.add_parser("backup")
    backup.add_argument("ledger", type=Path)
    backup.add_argument("output", type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("first", type=Path)
    compare.add_argument("second", type=Path)
    split = commands.add_parser("split")
    split.add_argument("dates", nargs="+")
    read_probe = commands.add_parser(
        "probe", help="capture read-only official contracts with local credentials"
    )
    read_probe.add_argument("--symbols", nargs="*", default=[])
    read_probe.add_argument("--output", type=Path, required=True)
    recovery = commands.add_parser(
        "recover", help="apply reviewed broker identities/fills/terminal evidence offline"
    )
    recovery.add_argument("--root", type=Path, default=Path("data"))
    recovery.add_argument("--date", type=date.fromisoformat, required=True)
    recovery.add_argument("--mode", choices=["mock", "live"], required=True)
    recovery.add_argument("evidence", type=Path)
    halt = commands.add_parser(
        "halt", help="latch entry halt; optionally request liquidation through the running service"
    )
    halt.add_argument("--root", type=Path, default=Path("data"))
    halt.add_argument("--date", type=date.fromisoformat, required=True)
    halt.add_argument("--mode", choices=["shadow", "mock", "live"], required=True)
    halt.add_argument("--liquidate", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = Settings.load(args.config)
        if args.command == "check":
            result = {
                "valid": True,
                "settings_hash": settings.fingerprint,
                "code_hash": code_fingerprint(),
                "mode": settings.mode,
                "live_enabled": settings.live_enabled,
                "costs_verified": settings.costs.verified,
            }
        elif args.command == "demo":
            args.output.mkdir(parents=True, exist_ok=True)
            events = args.output / "events.jsonl"
            session, instruments = demo_dataset(events)
            (args.output / "session.json").write_text(
                json.dumps({str(session.trading_date): session.model_dump(mode="json")}, indent=2),
                encoding="utf-8",
            )
            (args.output / "instruments.json").write_text(
                json.dumps([x.model_dump(mode="json") for x in instruments.values()], indent=2),
                encoding="utf-8",
            )
            result = replay(events, settings, session, instruments)
            write_report(args.output / "report.json", result)
            result = {
                key: result[key]
                for key in [
                    "performance_label",
                    "orders",
                    "round_trips",
                    "net_realized",
                    "signal_hash",
                    "qualification",
                    "closing_handoff_required",
                ]
            } | {"output": str(args.output)}
        elif args.command in {"replay", "stress", "run"}:
            instruments = load_instruments(args.instruments)
            session = Session.load(args.session, args.date, online=args.command == "run")
            if args.command == "run":
                if args.date != datetime.now(KST).date() or session.closed:
                    raise ValueError("online execution requires today's verified open session")
                body = settings.model_dump()
                body["mode"] = args.mode
                settings = Settings.model_validate(body)
                if settings.mode == "live":
                    if not args.qualification:
                        raise ValueError("live qualification artifact is required")
                    verify_live_gate(settings, args.qualification, args.qualification.parent)
                result = asyncio.run(
                    run_online(
                        settings,
                        session,
                        instruments,
                        args.root,
                        args.duration,
                        args.ranking,
                        args.qualification,
                    )
                )
            elif args.command == "replay":
                if settings.mode not in {"replay", "shadow"}:
                    raise ValueError("replay only accepts replay or shadow configurations")
                result = replay(args.events, settings, session, instruments)
                write_report(args.output, result)
                result = {
                    "report": str(args.output),
                    "signal_hash": result["signal_hash"],
                    "round_trips": result["round_trips"],
                }
            else:
                runs = {
                    name: replay(args.events, scenario_settings(settings, changes), session, instruments)
                    for name, changes in STRESS_SCENARIOS.items()
                }
                write_report(args.output, {"scenarios": runs, "qualification": "UNVALIDATED"})
                result = {"report": str(args.output), "scenarios": list(runs)}
        elif args.command == "backtest":
            start, end = backtest_period(datetime.now(KST).date(), args.start, args.end, args.days)
            output = args.output or Path(f"reports/runs/backtest-{start}_{end}")
            summary = backtest(
                settings,
                args.root,
                start,
                end,
                output,
                modes=args.modes,
                sessions=json.loads(args.session.read_text(encoding="utf-8")) if args.session else None,
                instruments=load_dated_instruments(args.instruments) if args.instruments else None,
                stress=args.stress,
                workers=args.workers,
                progress=lambda line: print(line, file=sys.stderr, flush=True),
            )
            coverage, baseline = summary["coverage"], summary["scenarios"]["baseline"]
            result = {
                "summary": str(output / "summary.json"),
                "period": summary["period"],
                "replayed_days": coverage["replayed_days"],
                "missing_weekdays": len(coverage["missing_weekdays"]),
                "skipped_days": coverage["skipped"],
                "failed_days": coverage["failed"],
                "round_trips": baseline["round_trips"],
                "net_realized": baseline["net_realized"],
                "max_drawdown": baseline["max_drawdown"],
                "bootstrap_lower_95": baseline["bootstrap_lower_95"],
                "qualification": summary["qualification"],
            }
            if not coverage["replayed_days"]:
                result["error"] = (
                    f"no replayable event logs under {summary['source']['namespace']}; "
                    "historical 10-level books must be recorded with run --mode collect/shadow"
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
        elif args.command == "parquet":
            count = to_parquet(args.events, args.output)
            result = {"count": count, "summary": parquet_summary(args.output)}
        elif args.command == "status":
            result = json.loads(args.path.read_text(encoding="utf-8"))
        elif args.command == "backup":
            if not args.ledger.is_file():
                raise ValueError("ledger does not exist")
            ledger = Ledger(args.ledger)
            try:
                ledger.backup(args.output)
            finally:
                ledger.close()
            result = {"backup": str(args.output)}
        elif args.command == "compare":
            first, second = [json.loads(p.read_text(encoding="utf-8")) for p in (args.first, args.second)]
            equal = first["signal_hash"] == second["signal_hash"]
            result = {"signals_equal": equal, "first": first["signal_hash"], "second": second["signal_hash"]}
            if not equal:
                print(json.dumps(result, indent=2))
                return 1
        elif args.command == "split":
            result = date_split(args.dates)
        elif args.command == "probe":
            from ..broker.kiwoom.mapping import symbol_code

            symbols = [symbol_code(s, "ka10001") for s in args.symbols]
            result = asyncio.run(probe(settings, symbols))
            write_report(args.output, result)
            result = {"output": str(args.output), "order_capability": False}
        elif args.command == "recover":
            namespace = args.root / settings.broker.environment / settings.broker.account_alias
            ledger_path = namespace / str(args.date) / args.mode / "ledger.sqlite"
            if not ledger_path.is_file():
                raise ValueError("recovery ledger does not exist")
            with AccountLock(namespace / "account.lock.runtime"):
                ledger = Ledger(ledger_path)
                try:
                    ledger.backup(ledger_path.with_name("before-recovery.sqlite"))
                    manager = OrderManager(ledger, settings, args.date)
                    issues = recover(manager, json.loads(args.evidence.read_text(encoding="utf-8")))
                    result = {"issues": issues, "state": ledger.get("account_state")}
                finally:
                    ledger.close()
        elif args.command == "halt":
            ledger_path = (
                args.root
                / settings.broker.environment
                / settings.broker.account_alias
                / str(args.date)
                / args.mode
                / "ledger.sqlite"
            )
            if not ledger_path.is_file():
                raise ValueError("active ledger does not exist")
            ledger = Ledger(ledger_path)
            try:
                with ledger.transaction():
                    ledger.set("operator_halt", True)
                    ledger.set("account_state", "EXIT_ONLY")
                    if args.liquidate:
                        ledger.set("emergency", True)
                    ledger.audit(
                        "operator_halt", {"liquidate": args.liquidate, "at": datetime.now(KST).isoformat()}
                    )
            finally:
                ledger.close()
            result = {"entries_halted": True, "liquidation_requested": args.liquidate}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
