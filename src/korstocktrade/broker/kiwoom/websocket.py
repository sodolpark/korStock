from __future__ import annotations

import asyncio
import json

from websockets.asyncio.client import connect

from .rest import KiwoomREST


class KiwoomWebSocket:
    def __init__(self, rest: KiwoomREST, symbols: list[str]):
        self.rest, self.symbols = rest, symbols
        host = "mockapi" if rest.config.environment == "mock" else "api"
        self.url = f"wss://{host}.kiwoom.com:10000/api/websocket"
        self.socket = None
        self.generation = 0
        self.stopping = False
        self.send_lock = asyncio.Lock()
        self.conditions: set[str] = set()

    async def send(self, message: dict):
        async with self.send_lock:
            if self.socket:
                await self.socket.send(json.dumps(message))

    async def subscribe(self, symbols: list[str]):
        removed = sorted(set(self.symbols) - set(symbols))
        added = sorted(set(symbols) - set(self.symbols))
        if removed:
            await self.send(
                {"trnm": "REMOVE", "grp_no": "1", "data": [{"item": removed, "type": ["0B", "0D"]}]}
            )
        if added:
            await self.send(
                {
                    "trnm": "REG",
                    "grp_no": "1",
                    "refresh": "1",
                    "data": [{"item": added, "type": ["0B", "0D"]}],
                }
            )
        self.symbols = list(symbols)

    async def condition_list(self):
        await self.send({"trnm": "CNSRLST"})

    async def condition(self, sequence: str, realtime: bool = True):
        if realtime and sequence not in self.conditions and len(self.conditions) >= 10:
            raise ValueError("condition subscription limit")
        await self.send(
            {"trnm": "CNSRREQ", "seq": sequence, "search_type": "1" if realtime else "0", "stex_tp": "K"}
        )
        if realtime:
            self.conditions.add(sequence)

    async def remove_condition(self, sequence: str):
        await self.send({"trnm": "CNSRCLR", "seq": sequence})
        self.conditions.discard(sequence)

    async def messages(self):
        delay = 1.0
        while not self.stopping:
            try:
                token = await self.rest.authenticate()
                async with connect(self.url, max_queue=64, ping_interval=20, ping_timeout=20) as socket:
                    self.socket = socket
                    await self.send({"trnm": "LOGIN", "token": token})
                    while True:
                        response = json.loads(await asyncio.wait_for(socket.recv(), 20))
                        if response.get("trnm") == "PING":
                            await self.send(response)
                            continue
                        if response.get("trnm") != "LOGIN" or response.get("return_code") != 0:
                            raise ConnectionError("websocket login failed")
                        break
                    self.generation += 1
                    await self.send(
                        {
                            "trnm": "REG",
                            "grp_no": "0",
                            "refresh": "1",
                            "data": [{"item": [""], "type": ["00", "04", "0s", "1h"]}],
                        }
                    )
                    if self.symbols:
                        await self.send(
                            {
                                "trnm": "REG",
                                "grp_no": "1",
                                "refresh": "1",
                                "data": [{"item": self.symbols, "type": ["0B", "0D"]}],
                            }
                        )
                    for condition in sorted(self.conditions):
                        await self.condition(condition)
                    delay = 1
                    yield {"_connection": "connected"}
                    while not self.stopping:
                        message = json.loads(await asyncio.wait_for(socket.recv(), 45))
                        if message.get("trnm") == "PING":
                            await self.send(message)
                        yield message
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                yield {"_connection": "disconnected", "error": type(exc).__name__}
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)
            finally:
                self.socket = None

    async def close(self):
        self.stopping = True
        if self.socket:
            await self.socket.close()
