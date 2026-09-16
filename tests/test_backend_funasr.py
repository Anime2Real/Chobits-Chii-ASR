"""tools/backend_funasr.py 测试：start 帧校验与引擎命令翻译（mock 引擎 WS）。"""
import asyncio
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import backend_funasr as backend  # noqa: E402


class FakeClient:
    """FastAPI WebSocket 替身：记录下发帧与关闭码。"""

    def __init__(self, start_frame):
        self._start = start_frame
        self.sent = []
        self.closed_with = None

    async def receive_json(self):
        return self._start

    async def receive(self):
        return {"type": "websocket.disconnect"}

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed_with = code


class FakeEngineConn:
    """引擎 WS 替身：记录命令，async with / async for 协议最小实现（立即 EOF）。"""

    def __init__(self):
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data):
        self.commands.append(data)

    def __aiter__(self):
        return self._empty()

    async def _empty(self):
        return
        yield  # pragma: no cover - 让本函数成为 async generator


@pytest.fixture
def fake_engine(monkeypatch):
    conn = FakeEngineConn()
    monkeypatch.setattr(backend.websockets, "connect", lambda url, **kw: conn)
    return conn


def run(coro):
    return asyncio.run(coro)


# --- _to_engine_commands --------------------------------------------------------

@pytest.mark.parametrize("lang,expected", [
    ("ja", ["START", "LANGUAGE:日本語"]),
    ("zh", ["START", "LANGUAGE:中文"]),
    ("en", ["START", "LANGUAGE:English"]),
    ("auto", ["START"]),
    ("", ["START"]),
    (None, ["START"]),
])
def test_to_engine_commands_language_whitelist(lang, expected):
    assert backend._to_engine_commands({"type": "start", "language": lang}) == expected


def test_to_engine_commands_rejects_ko():
    with pytest.raises(ValueError):
        backend._to_engine_commands({"type": "start", "language": "ko"})


def test_to_engine_commands_rejects_command_injection():
    # 引擎控制帧是纯文本命令：白名单外的值原样透传 = 客户端直接写引擎命令行
    with pytest.raises(ValueError):
        backend._to_engine_commands({"type": "start", "language": "ja\nHOTWORDS:x"})


# --- handle_realtime start 帧校验 -------------------------------------------------

def test_start_frame_first_frame_must_be_start():
    client = FakeClient({"type": "stop"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert client.closed_with == 1002
    assert client.sent[0]["type"] == "error"
    assert "start" in client.sent[0]["message"]


def test_start_frame_sample_rate_non_integer():
    client = FakeClient({"type": "start", "language": "ja", "sample_rate": "abc"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert client.closed_with == 1002
    assert client.sent[0]["type"] == "error"
    assert "sample_rate" in client.sent[0]["message"]


def test_start_frame_sample_rate_not_16k_rejected():
    client = FakeClient({"type": "start", "language": "ja", "sample_rate": 8000})
    run(backend.handle_realtime(client, "ws://fake"))
    assert client.closed_with == 1002
    assert "16kHz" in client.sent[0]["message"]


def test_start_frame_sample_rate_none_defaults_16000(fake_engine):
    # 未传 sample_rate → 缺省 16000，会话继续：START 命令发给假引擎
    client = FakeClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert not [f for f in client.sent if f.get("type") == "error"]
    assert "START" in fake_engine.commands
    assert "LANGUAGE:日本語" in fake_engine.commands


def test_start_frame_valid_16k(fake_engine):
    client = FakeClient({"type": "start", "language": "auto", "sample_rate": 16000})
    run(backend.handle_realtime(client, "ws://fake"))
    assert not [f for f in client.sent if f.get("type") == "error"]
    assert fake_engine.commands == ["START"]


def test_start_frame_language_ko_error_frame():
    client = FakeClient({"type": "start", "language": "ko", "sample_rate": 16000})
    run(backend.handle_realtime(client, "ws://fake"))
    assert client.closed_with == 1002
    assert client.sent[0]["type"] == "error"
    assert "ko" in client.sent[0]["message"]
