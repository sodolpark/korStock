import asyncio
import json
import time
from datetime import datetime
from datetime import time as clock_time
from pathlib import Path

from korstocktrade.app.service import run_online
from korstocktrade.config import Settings
from korstocktrade.market.session import KST, Session
from korstocktrade.storage.events import read_events


async def test_collect_service_has_no_order_capability(monkeypatch, tmp_path, settings, instrument):
    day = datetime.now(KST).date()
    session = Session(
        trading_date=day,
        open=clock_time(0),
        entry_start=clock_time(0),
        entry_end=clock_time(23, 50),
        exit_start=clock_time(23, 58),
        auction_start=clock_time(23, 59),
        close=clock_time(23, 59, 59),
        verified=True,
    )
    body = settings.model_dump()
    body["mode"] = "collect"
    settings = Settings.model_validate(body)
    instrument = instrument.model_copy(update={"verified_date": day})

    class Rest:
        def __init__(self, config):
            self.expires = time.time() + 3600

        async def authenticate(self):
            return "secret"

        async def close(self):
            pass

    class WebSocket:
        generation = 1

        def __init__(self, rest, symbols):
            self.symbols = symbols

        async def messages(self):
            yield {"_connection": "connected"}
            yield {"trnm": "REAL", "data": [{"type": "0s", "item": "", "values": {"215": "3"}}]}
            yield {
                "trnm": "REAL",
                "data": [{"type": "0B", "item": "005930", "values": {"10": "10000", "15": "+100"}}],
            }
            await asyncio.Event().wait()

        async def close(self):
            pass

    def no_order_adapter(*args, **kwargs):
        raise AssertionError("collect must never construct an order adapter")

    monkeypatch.setattr("korstocktrade.app.service.KiwoomREST", Rest)
    monkeypatch.setattr("korstocktrade.app.service.KiwoomWebSocket", WebSocket)
    monkeypatch.setattr("korstocktrade.app.service.KiwoomOrderAdapter", no_order_adapter)
    result = await run_online(settings, session, {instrument.symbol: instrument}, tmp_path, duration=0.01)
    events = list(read_events(Path(result["events"])))
    assert any(event.kind == "trade" for event in events)
    assert Path(result["report"]).is_file()
    run_id = Path(result["events"]).stem.removeprefix("events-")
    recorded = Path(result["events"]).parent
    assert Session.load(recorded / f"session-{run_id}.json", day) == session
    assert json.loads((recorded / f"instruments-{run_id}.json").read_text(encoding="utf-8")) == [
        instrument.model_dump(mode="json")
    ]
