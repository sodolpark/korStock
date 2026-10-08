from __future__ import annotations

import json
import os
from pathlib import Path

from ..models import Event

SECRET_KEYS = {
    "authorization",
    "appkey",
    "app_key",
    "secretkey",
    "secret_key",
    "token",
    "access_token",
    "refresh_token",
    "9201",
    "acnt_no",
    "account_number",
}


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if k.lower() in SECRET_KEYS else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(x) for x in value]
    return value


class Recorder:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents two sessions from silently mixing sequence spaces.
        self.file = path.open("x", encoding="utf-8", newline="\n")
        self.count = 0
        self.failed = False
        self.last_sequence = 0

    def write(self, event: Event):
        if event.sequence <= self.last_sequence:
            raise ValueError("non-increasing receive sequence")
        try:
            self.file.write(
                json.dumps(redact(event.model_dump(mode="json")), ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            self.file.flush()
            self.count += 1
            self.last_sequence = event.sequence
            if self.count % 100 == 0:
                os.fsync(self.file.fileno())
        except OSError:
            self.failed = True
            raise

    def close(self):
        if not self.file.closed:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def read_events(path: Path):
    previous = 0
    run_id = None
    mono = -1
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                event = Event.model_validate_json(line)
            except ValueError as exc:
                raise ValueError(f"invalid/incomplete event at line {number}") from exc
            if event.sequence <= previous or event.monotonic_ns < mono or run_id not in (None, event.run_id):
                raise ValueError(f"receive ordering/run violation at line {number}")
            previous, mono, run_id = event.sequence, event.monotonic_ns, event.run_id
            yield event


def to_parquet(source: Path, target: Path, batch_size: int = 10000) -> int:
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [
            ("sequence", pa.int64()),
            ("trading_date", pa.string()),
            ("symbol", pa.string()),
            ("kind", pa.string()),
            ("received_at", pa.string()),
            ("valid", pa.bool_()),
            ("event_json", pa.string()),
        ]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    batch = []
    with pq.ParquetWriter(target, schema, compression="zstd") as writer:
        for event in read_events(source):
            batch.append(
                {
                    "sequence": event.sequence,
                    "trading_date": str(event.trading_date),
                    "symbol": event.symbol,
                    "kind": event.kind,
                    "received_at": event.received_at.isoformat(),
                    "valid": event.valid,
                    "event_json": json.dumps(redact(event.model_dump(mode="json")), ensure_ascii=False),
                }
            )
            count += 1
            if len(batch) >= batch_size:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                batch.clear()
        if batch:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    return count


def parquet_summary(path: Path) -> list[dict]:
    import duckdb

    with duckdb.connect() as connection:
        rows = connection.execute(
            "SELECT kind, count(*), count(*) FILTER (WHERE NOT valid) FROM read_parquet(?) GROUP BY kind ORDER BY kind",
            [str(path)],
        ).fetchall()
    return [{"kind": kind, "events": count, "invalid": invalid} for kind, count, invalid in rows]
