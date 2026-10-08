from __future__ import annotations

import asyncio
import heapq
import itertools
import os
import time
from datetime import datetime
from pathlib import Path

import httpx

from ...config import BrokerConfig, Settings, verify_live_gate
from ...market.session import KST

BASE_URLS = {"mock": "https://mockapi.kiwoom.com", "live": "https://api.kiwoom.com"}
QUERY_PATHS = {
    "ka10032": "/api/dostk/rkinfo",
    "ka10075": "/api/dostk/acnt",
    "ka10076": "/api/dostk/acnt",
    "kt00018": "/api/dostk/acnt",
    "kt00001": "/api/dostk/acnt",
    "ka10099": "/api/dostk/stkinfo",
    "ka10100": "/api/dostk/stkinfo",
    "ka10001": "/api/dostk/stkinfo",
    "ka10054": "/api/dostk/stkinfo",
}
ORDER_IDS = {"kt10000", "kt10001", "kt10002", "kt10003"}


class BrokerError(RuntimeError):
    pass


class UncertainOrder(BrokerError):
    pass


class PriorityGate:
    def __init__(self, rate: float):
        self.interval = 1 / rate
        self.next_at = 0.0
        self.queue = []
        self.serial = itertools.count()
        self.worker = None

    async def acquire(self, priority: int = 10):
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self.queue, (priority, next(self.serial), future))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._drain())
        await future

    async def _drain(self):
        while self.queue:
            await asyncio.sleep(max(0, self.next_at - time.monotonic()))
            _, _, future = heapq.heappop(self.queue)
            if future.cancelled():
                continue
            self.next_at = time.monotonic() + self.interval
            future.set_result(None)

    async def close(self):
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for _, _, future in self.queue:
            future.cancel()


class KiwoomREST:
    def __init__(self, config: BrokerConfig, *, transport=None, credentials: tuple[str, str] | None = None):
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=BASE_URLS[config.environment], timeout=config.timeout, transport=transport
        )
        prefix = f"KIWOOM_{config.environment.upper()}_"
        self.credentials = credentials or (
            os.getenv(prefix + "APP_KEY", ""),
            os.getenv(prefix + "SECRET_KEY", ""),
        )
        self.token = ""
        self.expires = 0.0
        self.auth_lock = asyncio.Lock()
        self.query_gate = PriorityGate(config.query_per_second)
        self.order_gate = PriorityGate(config.order_per_second)
        self.tr_gates: dict[str, PriorityGate] = {}

    async def authenticate(self) -> str:
        async with self.auth_lock:
            if self.token and self.expires - time.time() > 60:
                return self.token
            key, secret = self.credentials
            if not key or not secret:
                raise BrokerError(f"missing {self.config.environment} credentials in environment")
            response = await self.client.post(
                "/oauth2/token", json={"grant_type": "client_credentials", "appkey": key, "secretkey": secret}
            )
            response.raise_for_status()
            body = response.json()
            if body.get("return_code", 0) != 0 or not body.get("token"):
                raise BrokerError("authentication rejected")
            self.token = body["token"]
            self.expires = (
                datetime.strptime(body["expires_dt"], "%Y%m%d%H%M%S").replace(tzinfo=KST).timestamp()
            )
            return self.token

    async def revoke(self):
        if self.token:
            key, secret = self.credentials
            response = await self.client.post(
                "/oauth2/revoke", json={"appkey": key, "secretkey": secret, "token": self.token}
            )
            response.raise_for_status()
            self.token, self.expires = "", 0

    async def _request(
        self,
        api: str,
        path: str,
        body: dict,
        *,
        order: bool = False,
        priority: int = 10,
        cont: str = "N",
        next_key: str = "",
    ) -> tuple[dict, httpx.Headers]:
        token = await self.authenticate()
        if self.config.environment == "mock":
            gate = self.tr_gates.setdefault(api, PriorityGate(self.config.mock_per_tr_second))
            await gate.acquire(priority)
        await (self.order_gate if order else self.query_gate).acquire(priority)
        try:
            response = await self.client.post(
                path,
                headers={
                    "api-id": api,
                    "authorization": f"Bearer {token}",
                    "cont-yn": cont,
                    "next-key": next_key,
                },
                json=body,
            )
            # No mutation retries: even a server error can arrive after broker acceptance.
            if order and response.status_code >= 500:
                raise UncertainOrder(f"order HTTP {response.status_code}; reconcile")
            response.raise_for_status()
            result = response.json()
            if "return_code" not in result:
                raise (
                    UncertainOrder("order response lacks status")
                    if order
                    else BrokerError("response lacks status")
                )
            if result["return_code"] != 0:
                raise BrokerError(f"{api} rejected, code={result['return_code']}")
            return result, response.headers
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as exc:
            if order:
                raise UncertainOrder(f"{api} response uncertain: {type(exc).__name__}") from exc
            raise

    async def query(self, api: str, body: dict, *, priority: int = 10, max_pages: int = 100) -> list[dict]:
        if api not in QUERY_PATHS:
            raise ValueError("not an allowed read-only API")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        pages, continuation, key, seen = [], "N", "", set()
        for _ in range(max_pages):
            for attempt in range(3):
                try:
                    result, headers = await self._request(
                        api, QUERY_PATHS[api], body, priority=priority, cont=continuation, next_key=key
                    )
                    break
                except (httpx.TransportError, httpx.HTTPStatusError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5 * 2**attempt)
            pages.append(result)
            if headers.get("cont-yn") != "Y":
                return pages
            key = headers.get("next-key", "")
            if not key or key in seen:
                raise BrokerError("invalid pagination cursor")
            seen.add(key)
            continuation = "Y"
        raise BrokerError("pagination limit exceeded; partial account snapshot rejected")

    async def ranking(self) -> list[str]:
        pages = await self.query(
            "ka10032", {"mrkt_tp": "000", "mang_stk_incls": "0", "stex_tp": "1"}, priority=20
        )
        return [row["stk_cd"] for page in pages for row in page.get("trde_prica_upper", [])]

    async def close(self):
        await self.client.aclose()
        for gate in [self.query_gate, self.order_gate, *self.tr_gates.values()]:
            await gate.close()


class KiwoomOrderAdapter:
    """Only injected for explicit mock/live modes; read-only services never receive this capability."""

    def __init__(
        self,
        rest: KiwoomREST,
        mode: str,
        *,
        settings: Settings | None = None,
        qualification: Path | None = None,
    ):
        if mode not in {"mock", "live"} or mode != rest.config.environment:
            raise ValueError("order adapter mode/environment mismatch")
        if mode == "live":
            if settings is None or settings.mode != "live" or qualification is None:
                raise ValueError("live order capability requires frozen qualification evidence")
            verify_live_gate(settings, qualification, qualification.parent)
        self.rest = rest

    async def submit(self, order) -> str:
        api = "kt10000" if order.side == "BUY" else "kt10001"
        body = {
            "dmst_stex_tp": "KRX",
            "stk_cd": order.symbol,
            "ord_qty": str(order.quantity),
            "ord_uv": str(order.limit_price),
            "trde_tp": "0",
            "cond_uv": "",
        }
        result, _ = await self.rest._request(
            api, "/api/dostk/ordr", body, order=True, priority=10 if order.side == "BUY" else 0
        )
        if not result.get("ord_no"):
            raise UncertainOrder("missing order number")
        return result["ord_no"]

    async def cancel(self, order) -> dict:
        result, _ = await self.rest._request(
            "kt10003",
            "/api/dostk/ordr",
            {
                "dmst_stex_tp": "KRX",
                "orig_ord_no": order.broker_id,
                "stk_cd": order.symbol,
                "cncl_qty": str(order.remaining),
            },
            order=True,
            priority=0,
        )
        return result

    async def amend(self, order, price: int) -> dict:
        # Runtime uses cancel-confirm-replace; amendment is available for contract qualification.
        result, _ = await self.rest._request(
            "kt10002",
            "/api/dostk/ordr",
            {
                "dmst_stex_tp": "KRX",
                "orig_ord_no": order.broker_id,
                "stk_cd": order.symbol,
                "mdfy_qty": str(order.remaining),
                "mdfy_uv": str(price),
                "mdfy_cond_uv": "",
            },
            order=True,
            priority=0,
        )
        return result
