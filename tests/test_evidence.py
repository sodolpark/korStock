import json
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


def test_all_64_scores_preserve_original_outlier():
    rows = json.loads(Path("tests/fixtures/evidence_scores.json").read_text())
    assert len(rows) == 64
    mismatches = []
    for row in rows:
        residual = Decimal(row["total"]) - sum(Decimal(x) for x in row["components"])
        if abs(residual) > Decimal("0.1"):
            mismatches.append((row["time"], residual))
    assert mismatches == [("09:13:09", Decimal("20.6"))]


def test_evidence_trades_arithmetic_only():
    rows = json.loads(Path("tests/fixtures/evidence_trades.json").read_text())
    assert len(rows) == 3
    for row in rows:
        fee = (Decimal(row["buy"]) * Decimal(".00015") + Decimal(row["sell"]) * Decimal(".00215")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        assert row["sell"] - row["buy"] - fee == row["net"]
    assert sum(r["net"] for r in rows) == 7477
