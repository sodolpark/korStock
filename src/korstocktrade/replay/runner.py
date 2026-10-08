import hashlib
from collections.abc import Iterable
from pathlib import Path

from ..app.core import TradingCore
from ..storage.events import read_events
from ..storage.ledger import Ledger
from .evaluate import report


def replay(paths: Path | Iterable[Path], settings, session, instruments, ledger: Ledger | None = None):
    """Replay one trading day's run logs in order; restarts share only the account ledger."""
    paths = [Path(paths)] if isinstance(paths, str | Path) else [Path(path) for path in paths]
    if not paths:
        raise ValueError("at least one event log is required")
    owned = ledger is None
    ledger = ledger or Ledger()
    core = None
    try:
        for path in paths:
            previous, core = core, TradingCore(settings, session, instruments, ledger)
            if previous:
                # Feature windows and confirmation timers restart like a new process; report totals carry on.
                core.signals, core.equity_curve = previous.signals, previous.equity_curve
                core.event_count, core.invalid_count = previous.event_count, previous.invalid_count
                core.gaps = previous.gaps
            for event in read_events(path):
                core.process(event)
        digests = []
        for path in paths:
            with path.open("rb") as stream:
                digests.append(hashlib.file_digest(stream, "sha256").hexdigest())
        digest = digests[0] if len(digests) == 1 else hashlib.sha256("\n".join(digests).encode()).hexdigest()
        return report(core, data_hash=digest, source=";".join(str(path) for path in paths))
    finally:
        if owned:
            ledger.close()
