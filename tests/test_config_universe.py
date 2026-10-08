import json
from datetime import timedelta

import pytest

from korstocktrade.config import Settings, verify_live_gate
from korstocktrade.market.universe import UniverseManager


def test_live_and_evidence_profiles_cannot_be_accidentally_enabled(settings):
    body = settings.model_dump()
    body["mode"] = "live"
    with pytest.raises(ValueError):
        Settings.model_validate(body)
    body["mode"] = "replay"
    body["profile"] = "evidence_ui"
    with pytest.raises(ValueError):
        Settings.model_validate(body)


def test_qualification_requires_actual_artifacts(tmp_path, settings):
    path = tmp_path / "qualification.json"
    path.write_text(json.dumps({"operational_pass": True}))
    with pytest.raises(ValueError, match="not all passed"):
        verify_live_gate(settings, path, tmp_path)


def test_universe_retains_positions_pending_and_warming(instrument, day, now):
    all_items = {f"00000{i}": instrument.model_copy(update={"symbol": f"00000{i}"}) for i in range(5)}
    universe = UniverseManager(capacity=3)
    assert universe.select(["000001", "000002", "000003"], all_items, day, now) == [
        "000001",
        "000002",
        "000003",
    ]
    chosen = universe.select(["000004"], all_items, day, now + 10, held=["000003"], pending=["000002"])
    assert chosen == ["000003", "000002", "000001"]
    chosen = universe.select(["000004"], all_items, day, now + 361, held=["000003"], pending=["000002"])
    assert chosen == ["000003", "000002", "000004"]


def test_unknown_product_and_stale_instrument_never_selected(instrument, day, now):
    universe = UniverseManager()
    bad = instrument.model_copy(update={"verified_date": day - timedelta(days=1)})
    assert universe.select([bad.symbol], {bad.symbol: bad}, day, now) == []
    assert universe.select(["000000"], {}, day, now) == []
