import json
import random
from datetime import date

import pytest

from korstocktrade.app.cli import main
from korstocktrade.replay.backtest import (
    backtest,
    backtest_period,
    load_dated_instruments,
    scenario_settings,
    summarize_period,
    turning_points,
)
from korstocktrade.replay.demo import demo_dataset
from korstocktrade.replay.evaluate import maximum_drawdown
from korstocktrade.replay.runner import replay
from korstocktrade.storage.events import Recorder, read_events


def record_day(namespace, day, sidecars=True, run_id="run"):
    root = namespace / day.isoformat() / "collect"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"events-{run_id}.jsonl"
    session, instruments = demo_dataset(path, day)
    if sidecars:
        (root / f"session-{run_id}.json").write_text(
            json.dumps({str(day): session.model_dump(mode="json")}), encoding="utf-8"
        )
        (root / f"instruments-{run_id}.json").write_text(
            json.dumps([x.model_dump(mode="json") for x in instruments.values()]), encoding="utf-8"
        )
    return path, session, instruments


def test_default_period_is_last_365_days_ending_yesterday():
    assert backtest_period(date(2026, 10, 9)) == (date(2025, 10, 9), date(2026, 10, 8))
    assert backtest_period(date(2026, 10, 9), end=date(2026, 3, 31), days=31) == (
        date(2026, 3, 1),
        date(2026, 3, 31),
    )
    with pytest.raises(ValueError, match="after its end"):
        backtest_period(date(2026, 10, 9), start=date(2026, 10, 9))


def test_period_backtest_replays_each_recorded_day_independently(tmp_path, settings):
    namespace = tmp_path / "data" / "mock" / "research"
    days = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
    path, session, instruments = [record_day(namespace, day) for day in days][0]
    record_day(namespace, date(2026, 10, 8), sidecars=False)
    output = tmp_path / "out"
    summary = backtest(settings, tmp_path / "data", date(2026, 10, 4), date(2026, 10, 9), output, workers=2)
    single = replay(path, scenario_settings(settings, {}), session, instruments)
    coverage, baseline = summary["coverage"], summary["scenarios"]["baseline"]
    assert coverage["replayed_days"] == 3
    # Sunday is not a missing trading day; Friday has no log; Thursday lacks dated inputs.
    assert coverage["missing_weekdays"] == ["2026-10-09"]
    assert coverage["skipped"] == {"2026-10-08": "missing_session"}
    assert baseline["round_trips"] == 3 * single["round_trips"] >= 3
    assert baseline["net_realized"] == pytest.approx(3 * single["net_realized"])
    equity = settings.risk.equity
    assert [row["equity"] for row in baseline["daily"]] == pytest.approx(
        [equity + n * single["net_realized"] for n in (1, 2, 3)]
    )
    assert baseline["by_month"]["2026-10"]["trades"] == baseline["round_trips"]
    assert "split" not in baseline and baseline["bootstrap_lower_95"] is None
    assert summary["qualification"] == "UNVALIDATED"
    report = json.loads((output / "days" / "2026-10-05.json").read_text(encoding="utf-8"))
    assert report["signal_hash"] == single["signal_hash"]
    assert json.loads((output / "summary.json").read_text(encoding="utf-8")) == summary


def test_restart_logs_of_one_day_share_the_account_ledger(tmp_path, settings):
    path = tmp_path / "events.jsonl"
    session, instruments = demo_dataset(path)
    events = list(read_events(path))
    cut = 2 + 200 * 9  # holding, after the profit floor was confirmed
    first, second = tmp_path / "events-a.jsonl", tmp_path / "events-b.jsonl"
    with Recorder(first) as recorder:
        for event in events[:cut]:
            recorder.write(event)
    head = events[cut]
    restart = [
        event.model_copy(update={"received_at": head.received_at, "monotonic_ns": head.monotonic_ns})
        for event in events[:2]
    ] + events[cut:]
    with Recorder(second) as recorder:
        for sequence, event in enumerate(restart, 1):
            recorder.write(event.model_copy(update={"run_id": "restart", "sequence": sequence}))
    alone = replay(first, settings, session, instruments)
    result = replay([first, second], settings, session, instruments)
    assert alone["remaining_positions"] and not alone["round_trips"]
    assert result["round_trips"] == 1 and not result["closing_handoff_required"]
    assert result["trades"][0]["exit_at"] > head.received_at.timestamp()
    assert result["quality"]["events"] == len(events) + 2
    assert result["source"] == f"{first};{second}"


def test_turning_points_keep_drawdown_and_split_uses_chronological_days(settings):
    rng = random.Random(3)
    for _ in range(200):
        values = [10_000.0]
        for _ in range(rng.randint(1, 60)):
            values.append(values[-1] + rng.choice([0, 0, 1, -1]) * rng.randint(1, 50))
        assert maximum_drawdown(turning_points(values)) == maximum_drawdown(values)
    equity = settings.risk.equity
    days = [
        {
            "date": f"2026-09-{n:02}",
            "net_realized": net,
            "orders": 2,
            "requested_quantity": 10,
            "filled_quantity": 10,
            "fees_and_taxes": 1.0,
            "trades": [{"symbol": "005930", "entry_at": 0, "net": net, "reasons": ["stop_loss"]}],
            "equity": [equity, equity + net],
            "events": 1,
            "invalid": 0,
            "gaps": 0,
            "closing_handoff_required": False,
            "account_state": "RUNNING",
        }
        for n, net in enumerate([100, -300, 200, 50, -50], 1)
    ]
    result = summarize_period(days, equity)
    assert result["net_realized"] == 0 and result["worst_day"] == -300
    assert result["max_drawdown"] == pytest.approx(300 / (equity + 100))
    assert result["profit_factor"] == pytest.approx(350 / 350)
    assert result["split"]["development"]["first_date"] == "2026-09-01"
    assert result["split"]["holdout"]["last_date"] == "2026-09-05"
    assert result["by_exit_reason"]["stop_loss"]["trades"] == 5


def test_cli_backtest_uses_explicit_dated_inputs_and_fails_without_logs(tmp_path, settings, capsys):
    namespace = tmp_path / "data" / "mock" / "research"
    day = date(2026, 10, 6)
    _, session, instruments = record_day(namespace, day, sidecars=False)
    sessions = tmp_path / "sessions.json"
    sessions.write_text(json.dumps({str(day): session.model_dump(mode="json")}), encoding="utf-8")
    rows = tmp_path / "instruments.json"
    other = instruments["005930"].model_copy(update={"verified_date": date(2026, 10, 7)})
    rows.write_text(
        json.dumps([x.model_dump(mode="json") for x in [*instruments.values(), other]]), encoding="utf-8"
    )
    assert set(load_dated_instruments(rows)) == {day, date(2026, 10, 7)}
    common = ["backtest", "--root", str(tmp_path / "data"), "--workers", "1", "--end", str(day)]
    output = tmp_path / "out"
    code = main(
        [
            *common,
            "--days",
            "2",
            "--session",
            str(sessions),
            "--instruments",
            str(rows),
            "--output",
            str(output),
        ]
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["replayed_days"] == 1 and printed["missing_weekdays"] == 1
    assert (output / "days" / f"{day}.json").is_file()
    assert main([*common, "--days", "2", "--output", str(tmp_path / "bare")]) == 1
    assert "missing_session" in json.loads(capsys.readouterr().out)["skipped_days"][str(day)]
