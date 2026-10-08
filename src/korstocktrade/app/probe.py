from __future__ import annotations

from ..broker.kiwoom.rest import KiwoomREST
from ..storage.events import redact


async def probe(settings, symbols: list[str]) -> dict:
    """Explicit read-only contract capture. No order adapter is ever constructed."""
    rest = KiwoomREST(settings.broker)
    result = {"environment": settings.broker.environment, "order_capability": False, "contracts": {}}
    try:
        await rest.authenticate()
        result["contracts"]["ka10032"] = await rest.query(
            "ka10032", {"mrkt_tp": "000", "mang_stk_incls": "0", "stex_tp": "1"}
        )
        for symbol in symbols:
            for api in ("ka10100", "ka10001"):
                result["contracts"][f"{api}:{symbol}"] = await rest.query(api, {"stk_cd": symbol})
        # Account endpoints are read only, but their successful response is not order qualification.
        result["contracts"]["kt00018"] = await rest.query(
            "kt00018", {"qry_tp": "2", "dmst_stex_tp": "KRX"}, priority=0
        )
        result["contracts"]["kt00001"] = await rest.query("kt00001", {"qry_tp": "3"}, priority=0)
        return redact(result)
    finally:
        await rest.close()
