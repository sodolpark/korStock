"""Offline load evidence, not a claim about Kiwoom/network throughput."""

import argparse
import hashlib
import json
import statistics
import time
from datetime import UTC, date, datetime
from pathlib import Path

from korstocktrade.app.core import TradingCore
from korstocktrade.broker.kiwoom.mapping import Normalizer
from korstocktrade.config import Settings, code_fingerprint
from korstocktrade.market.session import KST, Session
from korstocktrade.models import Book, Instrument, Level, Side, Trade
from korstocktrade.storage.events import Recorder
from korstocktrade.storage.ledger import Ledger


def benchmark(symbol_count: int, seconds: int, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    day = date(2026, 10, 8)
    settings = Settings.load("configs/research.toml")
    body = settings.model_dump()
    body["data"]["max_symbols"] = symbol_count
    settings = Settings.model_validate(body)
    session = Session(trading_date=day)
    instruments = {
        f"{i:06}": Instrument(
            symbol=f"{i:06}",
            board="KOSPI",
            product="COMMON",
            verified_date=day,
            listing_date=date(2000, 1, 1),
            lower_limit=7000,
            upper_limit=13000,
            normal=True,
        )
        for i in range(symbol_count)
    }
    ledger = Ledger(destination / "ledger.sqlite")
    core = TradingCore(settings, session, instruments, ledger)
    normalizer = Normalizer(f"benchmark-{symbol_count}", day)
    base = datetime(2026, 10, 8, 9, 0, tzinfo=KST).timestamp()
    durations = []
    book = Book(
        bids=[Level(price=10000 - i * 10, quantity=1000) for i in range(10)],
        asks=[Level(price=10010 + i * 10, quantity=1000) for i in range(10)],
    )
    trade = Trade(price=10000, quantity=10, side=Side.SELL)
    path = destination / "events.jsonl"
    started = time.perf_counter()
    with Recorder(path) as recorder:
        for kind, data in [("connection", {"status": "connected"}), ("market", {"phase": "CONTINUOUS"})]:
            event = normalizer.event(kind, datetime.fromtimestamp(base, UTC), 0, data=data)
            recorder.write(event)
            core.process(event)
        for quarter in range(seconds * 4):
            now = base + quarter / 4
            for symbol in instruments:
                for kind in ("book", "trade"):
                    event = normalizer.event(
                        kind,
                        datetime.fromtimestamp(now, UTC),
                        quarter * 250_000_000,
                        symbol=symbol,
                        **({"book": book} if kind == "book" else {"trade": trade}),
                    )
                    begin = time.perf_counter()
                    recorder.write(event)
                    core.process(event)
                    durations.append(time.perf_counter() - begin)
    elapsed = time.perf_counter() - started
    ordered = sorted(durations)
    result = {
        "code_hash": code_fingerprint(),
        "settings_hash": settings.fingerprint,
        "synthetic": True,
        "symbols": symbol_count,
        "simulated_seconds": seconds,
        "events": len(durations),
        "wall_seconds": elapsed,
        "events_per_second": len(durations) / elapsed,
        "processing_median_ms": statistics.median(durations) * 1000,
        "processing_p99_ms": ordered[int(len(ordered) * 0.99)] * 1000,
        "raw_bytes": path.stat().st_size,
        "invalid": core.invalid_count,
        "real_network_qualification": False,
    }
    ledger.close()
    with path.open("rb") as stream:
        result["data_hash"] = hashlib.file_digest(stream, "sha256").hexdigest()
    (destination / "benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", type=int, choices=[30, 50], default=30)
    parser.add_argument("--seconds", type=int, default=130)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.symbols, args.seconds, args.output), indent=2))
