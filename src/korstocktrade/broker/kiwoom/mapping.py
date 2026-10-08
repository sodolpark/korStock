from __future__ import annotations

import re
from datetime import UTC, datetime

from ...models import Book, Event, Level, Side, Trade
from ...storage.events import redact


def number(value, *, absolute: bool = False) -> int:
    if value is None or str(value).strip() == "":
        raise ValueError("missing numeric field")
    result = int(str(value).replace(",", "").strip())
    return abs(result) if absolute else result


def symbol_code(value: str, api: str) -> str:
    value = value.strip()
    if value.endswith(("_NX", "_AL")):
        raise ValueError("non-KRX symbol")
    # Product prefix is documented for balance/condition responses only.
    if api in {"kt00018", "ka10171", "ka10172", "ka10173", "00", "04"} and re.fullmatch(r"A\d{6}", value):
        value = value[1:]
    if not re.fullmatch(r"\d{6}", value):
        raise ValueError("invalid KRX symbol")
    return value


class Normalizer:
    def __init__(self, run_id: str, trading_date):
        self.run_id, self.day = run_id, trading_date
        self.sequence = 0

    def event(self, kind: str, at: datetime, mono: int, generation: int = 0, **kwargs) -> Event:
        self.sequence += 1
        return Event(
            run_id=self.run_id,
            sequence=self.sequence,
            generation=generation,
            trading_date=self.day,
            received_at=at.astimezone(UTC),
            monotonic_ns=mono,
            kind=kind,
            **kwargs,
        )

    def parse(self, message: dict, at: datetime, mono: int, generation: int) -> list[Event]:
        events = []
        if message.get("trnm") != "REAL":
            return [self.event("raw", at, mono, generation, api=message.get("trnm", ""), raw=redact(message))]
        for row in message.get("data", []):
            api, values = row.get("type", ""), row.get("values", {})
            symbol = ""
            try:
                raw_symbol = row.get("item") or values.get("9001", "")
                if raw_symbol:
                    symbol = symbol_code(raw_symbol, api)
                if api in {"0B", "0D", "00", "04", "1h"} and not symbol:
                    raise ValueError("missing instrument code")
                if values.get("2134") not in (None, "", "1"):
                    raise ValueError("non-KRX exchange field")
                payload = {
                    "api": api,
                    "symbol": symbol,
                    "raw": redact(row),
                    "exchange_time": values.get("20") or values.get("908") or values.get("21"),
                }
                if api == "0B":
                    volume = number(values["15"])
                    side = Side.BUY if volume > 0 else Side.SELL if volume < 0 else Side.UNKNOWN
                    trade = Trade(
                        price=number(values["10"], absolute=True),
                        quantity=abs(volume),
                        side=side,
                        cumulative_quantity=number(values["13"], absolute=True) if values.get("13") else None,
                        api_strength=float(values["228"]) if values.get("228") else None,
                    )
                    if values.get("290") not in (None, "", "2"):
                        raise ValueError("non-continuous trade session")
                    events.append(self.event("trade", at, mono, generation, trade=trade, **payload))
                elif api == "0D":
                    bids = [
                        Level(
                            price=number(values[str(51 + i)], absolute=True),
                            quantity=number(values[str(71 + i)], absolute=True),
                        )
                        for i in range(10)
                    ]
                    asks = [
                        Level(
                            price=number(values[str(41 + i)], absolute=True),
                            quantity=number(values[str(61 + i)], absolute=True),
                        )
                        for i in range(10)
                    ]
                    book = Book(bids=bids, asks=asks)
                    events.append(
                        self.event(
                            "book",
                            at,
                            mono,
                            generation,
                            book=book,
                            valid=book.valid,
                            issues=[] if book.valid else ["invalid_book"],
                            **payload,
                        )
                    )
                elif api == "0s":
                    phase = {
                        "0": "PREOPEN",
                        "3": "CONTINUOUS",
                        "2": "AUCTION",
                        "4": "CLOSED",
                        "8": "CLOSED",
                    }.get(values.get("215"), "UNKNOWN")
                    events.append(
                        self.event("market", at, mono, generation, data={"phase": phase}, **payload)
                    )
                elif api == "1h":
                    code = values.get("9068", "")
                    # 9068=2 means dynamic VI, NOT release. Require post-release market evidence.
                    events.append(
                        self.event(
                            "vi",
                            at,
                            mono,
                            generation,
                            data={"active": True, "code": code, "release_time": values.get("1224", "")},
                            **payload,
                        )
                    )
                elif api == "00":
                    events.append(self.event("order", at, mono, generation, data=redact(values), **payload))
                elif api == "04":
                    events.append(self.event("balance", at, mono, generation, data=redact(values), **payload))
                else:
                    events.append(self.event("raw", at, mono, generation, **payload))
            except (KeyError, ValueError, TypeError) as exc:
                events.append(
                    self.event(
                        "quality",
                        at,
                        mono,
                        generation,
                        symbol=symbol,
                        api=api,
                        raw=redact(row),
                        valid=False,
                        issues=[f"{type(exc).__name__}: {exc}"],
                    )
                )
        return events
