import asyncio
import json
import time

import httpx
import pytest

from korstocktrade.broker.kiwoom.rest import (
    BrokerError,
    KiwoomOrderAdapter,
    KiwoomREST,
    PriorityGate,
    UncertainOrder,
)
from korstocktrade.config import BrokerConfig
from korstocktrade.models import Order, Side


def fake_rest(handler, environment="mock"):
    rest = KiwoomREST(
        BrokerConfig(environment=environment),
        transport=httpx.MockTransport(handler),
        credentials=("test", "test"),
    )
    rest.token, rest.expires = "test-token", time.time() + 3600
    return rest


async def test_read_only_class_rejects_order_api():
    def handler(request):
        raise AssertionError("must not send")

    rest = fake_rest(handler)
    try:
        with pytest.raises(ValueError, match="read-only"):
            await rest.query("kt10000", {})
        with pytest.raises(ValueError, match="mismatch"):
            KiwoomOrderAdapter(rest, "shadow")
    finally:
        await rest.close()


async def test_live_adapter_cannot_bypass_qualification():
    rest = fake_rest(lambda request: None, environment="live")
    try:
        with pytest.raises(ValueError, match="qualification"):
            KiwoomOrderAdapter(rest, "live")
    finally:
        await rest.close()


async def test_order_timeout_is_not_retried(day, now):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "mockapi.kiwoom.com"
        raise httpx.ReadTimeout("injected", request=request)

    rest = fake_rest(handler)
    order = Order(
        intent_id="one",
        trading_date=day,
        account="research",
        symbol="005930",
        side=Side.BUY,
        quantity=1,
        limit_price=10000,
        created_at=now,
    )
    try:
        with pytest.raises(UncertainOrder):
            await KiwoomOrderAdapter(rest, "mock").submit(order)
        assert len(requests) == 1
        assert json.loads(requests[0].content)["dmst_stex_tp"] == "KRX"
    finally:
        await rest.close()


async def test_pagination_market_and_priority_headers():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["api-id"] == "ka10032"
        assert json.loads(request.content)["stex_tp"] == "1"
        if len(requests) == 1:
            return httpx.Response(
                200,
                headers={"cont-yn": "Y", "next-key": "next"},
                json={"return_code": 0, "trde_prica_upper": [{"stk_cd": "005930"}]},
            )
        assert request.headers["next-key"] == "next"
        return httpx.Response(200, json={"return_code": 0, "trde_prica_upper": [{"stk_cd": "000660"}]})

    rest = fake_rest(handler)
    try:
        assert await rest.ranking() == ["005930", "000660"]
    finally:
        await rest.close()


async def test_repeated_page_cursor_rejected():
    rest = fake_rest(
        lambda request: httpx.Response(
            200, headers={"cont-yn": "Y", "next-key": "loop"}, json={"return_code": 0}
        )
    )
    try:
        with pytest.raises(BrokerError, match="cursor"):
            await rest.query("kt00018", {"dmst_stex_tp": "KRX"})
    finally:
        await rest.close()


async def test_auth_expiry_and_secret_not_in_errors():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"return_code": 0, "token": "private-token", "expires_dt": "20991231120000"}
        )

    rest = KiwoomREST(
        BrokerConfig(), transport=httpx.MockTransport(handler), credentials=("private-key", "private-secret")
    )
    try:
        assert await rest.authenticate() == "private-token"
        assert await rest.authenticate() == "private-token"
        assert len(calls) == 1 and calls[0].url.path == "/oauth2/token"
    finally:
        await rest.close()


async def test_priority_gate_serializes_and_prioritizes_queued_cancels():
    gate = PriorityGate(100)
    order = []
    await gate.acquire()

    async def take(name, priority):
        await gate.acquire(priority)
        order.append((name, time.monotonic()))

    try:
        await asyncio.gather(take("buy", 10), take("cancel", 0), take("sell", 1))
        assert [name for name, _ in order] == ["cancel", "sell", "buy"]
        assert order[-1][1] - order[0][1] >= 0.019
    finally:
        await gate.close()
