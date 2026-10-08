import json

import pytest

from korstocktrade.broker.kiwoom.websocket import KiwoomWebSocket


class RestStub:
    class Config:
        environment = "mock"

    config = Config()

    async def authenticate(self):
        return "private-token"


class SocketStub:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        try:
            return json.dumps(next(self.frames))
        except StopIteration as exc:
            raise ConnectionError("injected disconnect") from exc

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


async def test_login_heartbeat_subscription_and_disconnect(monkeypatch):
    socket = SocketStub([{"trnm": "PING"}, {"trnm": "LOGIN", "return_code": 0}, {"trnm": "PING"}])
    monkeypatch.setattr("korstocktrade.broker.kiwoom.websocket.connect", lambda *a, **kw: socket)
    websocket = KiwoomWebSocket(RestStub(), ["005930"])
    stream = websocket.messages()
    try:
        assert (await anext(stream))["_connection"] == "connected"
        assert (await anext(stream))["trnm"] == "PING"
        assert (await anext(stream))["_connection"] == "disconnected"
        assert socket.sent[0] == {"trnm": "LOGIN", "token": "private-token"}
        assert len([m for m in socket.sent if m["trnm"] == "PING"]) == 2
        assert any(m.get("data", [{}])[0].get("item") == ["005930"] for m in socket.sent)
    finally:
        await stream.aclose()
        await websocket.close()


async def test_condition_limits_and_krx_only_requests():
    websocket = KiwoomWebSocket(RestStub(), [])
    socket = SocketStub([])
    websocket.socket = socket
    for i in range(10):
        await websocket.condition(str(i))
    with pytest.raises(ValueError, match="limit"):
        await websocket.condition("11")
    assert all(m["stex_tp"] == "K" for m in socket.sent)
    await websocket.remove_condition("0")
    await websocket.condition("11")
    assert len(websocket.conditions) == 10
