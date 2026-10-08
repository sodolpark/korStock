from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..broker.kiwoom.mapping import Normalizer, number
from ..broker.kiwoom.rest import BrokerError, KiwoomOrderAdapter, KiwoomREST, UncertainOrder
from ..broker.kiwoom.websocket import KiwoomWebSocket
from ..config import Settings
from ..execution.orders import ReconciliationRequired
from ..execution.reconcile import Reconciler, fetch_snapshot
from ..market.universe import UniverseManager
from ..models import Fill, Side
from ..replay.evaluate import report, write_report
from ..storage.events import Recorder
from ..storage.ledger import Ledger
from ..storage.lock import AccountLock
from .core import TradingCore


def apply_notification(core: TradingCore, event):
    """Use uniquely identified unit fills. Never add REST cumulative or balance quantities."""
    if event.kind == "balance":
        core.ledger.set("reconcile_requested", True)
        return
    data = event.data
    broker_id = data.get("9203", "")
    original_id = data.get("904", "")
    order = core.ledger.by_broker_id(broker_id)
    status = data.get("913", "")
    cancel_notice = "취소" in data.get("905", "")
    if cancel_notice:
        order = core.ledger.by_broker_id(original_id)
        if order and status in {"확인", "취소"}:
            core.orders.canceled(order.intent_id, core.now)
            return
    if order is None:
        core.orders.halt("unknown_external_order_notification")
        core.ledger.audit("unmatched_order", {"sequence": event.sequence, "data": data})
        core.ledger.set("reconcile_requested", True)
        return
    if status == "거부":
        core.orders.reject(order.intent_id, data.get("919", "rejected"))
        return
    if data.get("909"):
        # 915/914 are unit execution quantity/price. Missing identity or quantity is not guessed.
        quantity = number(data.get("915"))
        price = number(data.get("914"), absolute=True)
        side = {"1": Side.SELL, "2": Side.BUY}.get(data.get("907"), Side.UNKNOWN)
        fee_rate = (
            core.settings.costs.buy_fee
            if side == Side.BUY
            else core.settings.costs.sell_fee + core.settings.costs.sell_tax
        )
        core.orders.fill(
            Fill(
                trading_date=event.trading_date,
                account=core.settings.broker.account_alias,
                broker_id=broker_id,
                execution_id=data["909"],
                symbol=event.symbol,
                side=side,
                quantity=quantity,
                price=price,
                at=core.now,
                fee=quantity * price * fee_rate,
            )
        )
    if data.get("902") is not None and number(data["902"]) != core.ledger.order(order.intent_id).remaining:
        core.ledger.set("reconcile_requested", True)


async def run_online(
    settings: Settings,
    session,
    instruments,
    root: Path,
    duration: float | None = None,
    ranking: bool = False,
    qualification: Path | None = None,
):
    if settings.mode not in {"collect", "shadow", "mock", "live"}:
        raise ValueError("online mode required")
    namespace = root / settings.broker.environment / settings.broker.account_alias
    run_id = uuid.uuid4().hex
    day_root = namespace / str(session.trading_date) / settings.mode
    event_path = day_root / f"events-{run_id}.jsonl"
    report_path = day_root / f"report-{run_id}.json"
    lock_path = namespace / "account.lock.runtime"
    with AccountLock(lock_path):
        ledger = Ledger(day_root / "ledger.sqlite")
        # Dated inputs travel with the log so a later period backtest replays the same metadata.
        (day_root / f"session-{run_id}.json").write_text(
            json.dumps({str(session.trading_date): session.model_dump(mode="json")}, indent=2),
            encoding="utf-8",
        )
        (day_root / f"instruments-{run_id}.json").write_text(
            json.dumps([x.model_dump(mode="json") for x in instruments.values()], indent=2),
            encoding="utf-8",
        )
        core = TradingCore(settings, session, instruments, ledger)
        rest = KiwoomREST(settings.broker)
        order_adapter = (
            KiwoomOrderAdapter(rest, settings.mode, settings=settings, qualification=qualification)
            if settings.mode in {"mock", "live"}
            else None
        )
        universe = UniverseManager(settings.data.max_symbols, settings.data.retention_seconds)
        symbols = universe.select(
            list(instruments),
            instruments,
            session.trading_date,
            time.time(),
            held=[p.symbol for p in ledger.positions() if p.quantity],
            pending=[o.symbol for o in ledger.orders(active=True)],
        )
        websocket = KiwoomWebSocket(rest, symbols)
        normalizer = Normalizer(run_id, session.trading_date)
        queue = asyncio.Queue(maxsize=settings.data.queue_size)
        ingress_lock = asyncio.Lock()
        overflow = False
        recorder = None
        stopping = asyncio.Event()

        async def enqueue(event):
            nonlocal overflow
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                overflow = True
                # Preserve account updates with backpressure; lost market events are flagged.
                if event.kind in {"order", "balance", "connection"}:
                    await queue.put(event)

        async def receive():
            async for message in websocket.messages():
                async with ingress_lock:
                    at, mono = datetime.now(UTC), time.monotonic_ns()
                    if "_connection" in message:
                        events = [
                            normalizer.event(
                                "connection",
                                at,
                                mono,
                                websocket.generation,
                                data={"status": message["_connection"], "error": message.get("error")},
                            )
                        ]
                    else:
                        events = normalizer.parse(message, at, mono, websocket.generation)
                    for event in events:
                        await enqueue(event)
                if stopping.is_set():
                    return

        async def timer():
            while not stopping.is_set():
                await asyncio.sleep(1)
                async with ingress_lock:
                    await enqueue(
                        normalizer.event(
                            "timer", datetime.now(UTC), time.monotonic_ns(), websocket.generation
                        )
                    )

        async def execute(commands):
            if not order_adapter:
                return
            # Risk-reducing actions precede new buys.
            commands.sort(key=lambda item: (item[0] != "cancel", ledger.order(item[1]).side == Side.BUY))
            for action, intent in commands:
                order = ledger.order(intent)
                try:
                    if action == "submit":
                        dispatch_now = core.origin_wall + (time.monotonic_ns() - core.origin_mono) / 1e9
                        if order.side == Side.BUY:
                            feature = core.features.get(order.symbol)
                            fresh = (
                                feature
                                and feature.book
                                and feature.book.valid
                                and dispatch_now - feature.book_at <= settings.data.book_fresh_seconds
                            )
                            if (
                                not fresh
                                or not core.session.entry_allowed(dispatch_now)
                                or core.quality_block
                                or ledger.get("account_state") != "RUNNING"
                                or order.symbol in core.vi
                            ):
                                core.orders.reject(intent, "entry_invalid_before_dispatch")
                                continue
                        ledger.audit(
                            "order_dispatch",
                            {
                                "intent_id": intent,
                                "evaluated_at": order.created_at,
                                "dispatched_at": dispatch_now,
                            },
                        )
                        core.orders.sent(intent)
                        broker_id = await order_adapter.submit(order)
                        acknowledged_at = core.origin_wall + (time.monotonic_ns() - core.origin_mono) / 1e9
                        core.orders.acknowledge(intent, broker_id, acknowledged_at)
                    else:
                        response = await order_adapter.cancel(order)
                        ledger.audit(
                            "cancel_ack", {"intent_id": intent, "at": core.now, "response": response}
                        )
                        # REST acceptance is not a terminal cancellation. Wait for notification/reconciliation.
                except UncertainOrder as exc:
                    core.orders.unknown(intent, str(exc))
                    ledger.set("reconcile_requested", True)
                except BrokerError as exc:
                    if action == "submit":
                        core.orders.reject(intent, str(exc))
                    else:
                        core.orders.unknown(intent, "cancel rejected; reconcile")
                except Exception as exc:
                    core.orders.unknown(intent, f"order path exception:{type(exc).__name__}")
                    ledger.set("reconcile_requested", True)

        tasks = []
        start = time.monotonic()
        next_reconcile = next_ranking = 0.0
        storage_failed = False
        try:
            await rest.authenticate()
            recorder = Recorder(event_path)
            tasks = [asyncio.create_task(receive()), asyncio.create_task(timer())]
            while duration is None or time.monotonic() - start < duration:
                event = await asyncio.wait_for(queue.get(), timeout=50)
                if overflow:
                    event.data["capture_gap"] = True
                    ledger.audit("data_gap", {"sequence": event.sequence, "reason": "queue_overflow"})
                    overflow = False
                lag = (time.monotonic_ns() - event.monotonic_ns) / 1e9
                if lag > settings.data.max_processing_lag:
                    event.data["processing_lag_block"] = True
                if not storage_failed:
                    try:
                        recorder.write(event)
                    except OSError:
                        storage_failed = True
                        core.block("raw_storage_failure")
                if event.kind in {"order", "balance"} and order_adapter:
                    # Set clock first; account fills precede this event's strategy evaluation.
                    if core.origin_mono is not None:
                        core.now = core.origin_wall + (event.monotonic_ns - core.origin_mono) / 1e9
                    try:
                        apply_notification(core, event)
                    except (ReconciliationRequired, ValueError) as exc:
                        core.orders.halt(str(exc))
                        ledger.set("reconcile_requested", True)
                commands = core.process(event)
                await execute(commands)
                now = time.monotonic()
                if (
                    core.connected
                    and order_adapter
                    and (now >= next_reconcile or ledger.get("reconcile_requested", False))
                ):
                    try:
                        snapshot = await fetch_snapshot(rest, core.now)
                        issues = Reconciler(core.orders).reconcile(snapshot)
                        if (
                            not issues
                            and not storage_failed
                            and queue.qsize() < settings.data.queue_size // 2
                        ):
                            core.quality_block = False
                    except Exception as exc:
                        core.orders.halt(f"reconciliation_failed:{type(exc).__name__}")
                    ledger.set("reconcile_requested", False)
                    next_reconcile = now + 15
                if core.connected and ranking and now >= next_ranking:
                    try:
                        ranked = await rest.ranking()
                        selected = universe.select(
                            ranked,
                            instruments,
                            session.trading_date,
                            core.now,
                            held=[p.symbol for p in ledger.positions() if p.quantity],
                            pending=[o.symbol for o in ledger.orders(active=True)],
                            progressing=[
                                s
                                for s, state in core.strategy.entries.items()
                                if state.stage in {"STAGE1", "STAGE2"}
                            ],
                        )
                        await websocket.subscribe(selected)
                        # Nonblocking ingress: never wait on a producer holding a full account queue.
                        if not ingress_lock.locked():
                            async with ingress_lock:
                                await enqueue(
                                    normalizer.event(
                                        "universe",
                                        datetime.now(UTC),
                                        time.monotonic_ns(),
                                        websocket.generation,
                                        data={
                                            "symbols": selected,
                                            "ranking": ranked,
                                            "history": list(universe.history),
                                        },
                                    )
                                )
                        universe.history.clear()
                    except Exception as exc:
                        ledger.audit("ranking_error", {"type": type(exc).__name__})
                    next_ranking = now + settings.data.ranking_interval
                if event.kind == "timer":
                    status = {
                        "kst": session.local(core.now).isoformat(),
                        "mode": settings.mode,
                        "state": ledger.get("account_state"),
                        "connected": core.connected,
                        "subscriptions": len(websocket.symbols),
                        "queue": queue.qsize(),
                        "processing_lag": round(lag, 3),
                        "candidates": {
                            s: {"stage": state.stage, "reason": state.reason}
                            for s, state in core.strategy.entries.items()
                        },
                        "positions": {p.symbol: p.quantity for p in ledger.positions() if p.quantity},
                        "open_orders": len(ledger.orders(active=True)),
                        "net_pnl": ledger.get("net_pnl"),
                        "data_age": {
                            s: round(core.now - f.book_at, 2) if f.book else None
                            for s, f in core.features.items()
                        },
                        "token_expires_kst": datetime.fromtimestamp(
                            rest.expires, session.local(core.now).tzinfo
                        ).isoformat(),
                    }
                    try:
                        (day_root / "status.json").write_text(
                            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    except OSError:
                        core.block("status_storage_failure")
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception():
                        raise task.exception()
                if (
                    session.local(core.now).date() != session.trading_date
                    or session.local(core.now).time() >= session.close
                ):
                    break
        finally:
            stopping.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await websocket.close()
            await rest.close()
            if recorder:
                try:
                    recorder.close()
                except OSError:
                    storage_failed = True
            if event_path.exists():
                with event_path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                result = report(core, data_hash=digest, source=str(event_path))
                result["quality"]["storage_failed"] = storage_failed
                result["quality"]["unprocessed_at_shutdown"] = queue.qsize()
                write_report(report_path, result)
            ledger.backup(day_root / f"checkpoint-{run_id}.sqlite")
            ledger.close()
        return {"events": str(event_path), "report": str(report_path)}
