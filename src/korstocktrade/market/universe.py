import json
from dataclasses import dataclass
from pathlib import Path

from ..models import Instrument


def load_instruments(path: Path) -> dict[str, Instrument]:
    records = json.loads(path.read_text(encoding="utf-8"))
    instruments = [Instrument.model_validate(row) for row in records]
    if len({row.symbol for row in instruments}) != len(instruments):
        raise ValueError("duplicate instrument metadata")
    return {row.symbol: row for row in instruments}


@dataclass
class Candidate:
    symbol: str
    joined: float
    reason: str
    ready: bool = False


class UniverseManager:
    def __init__(self, capacity: int = 30, retention: float = 360):
        self.capacity = capacity
        self.retention = retention
        self.candidates: dict[str, Candidate] = {}
        self.history: list[dict] = []

    def select(
        self,
        ranked: list[str],
        instruments: dict[str, Instrument],
        day,
        now: float,
        held: list[str] = (),
        pending: list[str] = (),
        progressing: list[str] = (),
    ) -> list[str]:
        protected = list(dict.fromkeys([*held, *pending]))
        if len(protected) > self.capacity:
            raise RuntimeError("protected subscriptions exceed capacity")
        retained = [s for s, c in self.candidates.items() if now - c.joined < self.retention]
        pool = list(dict.fromkeys([*protected, *progressing, *retained, *ranked]))
        chosen = []
        for symbol in pool:
            if symbol not in protected and (
                symbol not in instruments or not instruments[symbol].eligible(day)
            ):
                continue
            if len(chosen) == self.capacity:
                break
            chosen.append(symbol)
            if symbol not in self.candidates:
                self.candidates[symbol] = Candidate(
                    symbol, now, "position" if symbol in protected else "ranking"
                )
                self.history.append(
                    {
                        "at": now,
                        "symbol": symbol,
                        "action": "join",
                        "rank": ranked.index(symbol) + 1 if symbol in ranked else None,
                    }
                )
        for symbol in set(self.candidates) - set(chosen):
            self.history.append(
                {"at": now, "symbol": symbol, "action": "leave", "reason": "capacity_or_ineligible"}
            )
            del self.candidates[symbol]
        return chosen
