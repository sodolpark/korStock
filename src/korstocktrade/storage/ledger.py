from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..models import TERMINAL, Order, Position

TERMINAL_FILTER = "SELECT body FROM orders WHERE json_extract(body, '$.state') NOT IN ({})".format(
    ",".join(f"'{state}'" for state in sorted(TERMINAL))
)


class Ledger:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.mutex = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, broker_id TEXT, body TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS broker_order ON orders(broker_id) WHERE broker_id != '';
            CREATE TABLE IF NOT EXISTS fills (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS positions (symbol TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, body TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        with self.mutex:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def put_order(self, order: Order):
        self.db.execute(
            "INSERT INTO orders VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET broker_id=excluded.broker_id,body=excluded.body",
            (order.intent_id, order.broker_id, order.model_dump_json()),
        )

    def orders(self, active: bool = False) -> list[Order]:
        # Terminal orders accumulate all day; skip them before model parsing on every event.
        query = "SELECT body FROM orders ORDER BY rowid"
        if active:
            query = TERMINAL_FILTER + " ORDER BY rowid"
        return [Order.model_validate_json(row[0]) for row in self.db.execute(query)]

    def order(self, intent_id: str) -> Order:
        row = self.db.execute("SELECT body FROM orders WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            raise KeyError(intent_id)
        return Order.model_validate_json(row[0])

    def by_broker_id(self, broker_id: str) -> Order | None:
        row = self.db.execute("SELECT body FROM orders WHERE broker_id=?", (broker_id,)).fetchone()
        return Order.model_validate_json(row[0]) if row else None

    def put_position(self, position: Position):
        self.db.execute(
            "INSERT INTO positions VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET body=excluded.body",
            (position.symbol, position.model_dump_json()),
        )

    def position(self, symbol: str) -> Position:
        row = self.db.execute("SELECT body FROM positions WHERE symbol=?", (symbol,)).fetchone()
        return Position.model_validate_json(row[0]) if row else Position(symbol=symbol)

    def positions(self) -> list[Position]:
        return [
            Position.model_validate_json(row[0])
            for row in self.db.execute("SELECT body FROM positions ORDER BY symbol")
        ]

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT body FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        self.db.execute(
            "INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body",
            (key, json.dumps(value, sort_keys=True)),
        )

    def audit(self, kind: str, body: dict):
        self.db.execute("INSERT INTO audit(kind,body) VALUES(?,?)", (kind, json.dumps(body, sort_keys=True)))

    def audits(self, kind: str) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT body FROM audit WHERE kind=? ORDER BY seq", (kind,))
        ]

    def backup(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as target:
            self.db.backup(target)

    def close(self):
        self.db.close()
