"""tools/backend_funasr.py 测试：start 帧校验、引擎命令翻译、WS 背压（mock 引擎 WS）。"""
import asyncio
import json
import sys
import os

import pytest
import websockets

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import backend_funasr as backend  # noqa: E402


class FakeClient:
    """FastAPI WebSocket 替身：记录下发帧与关闭码；frames 可脚本化 receive() 返回。"""

    def __init__(self, start_frame, frames=None):
        self._start = start_frame
        self._frames = list(frames or [])
        self.sent = []
        self.closed_with = None
        self.close_codes = []

    async def receive_json(self):
        return self._start

    async def receive(self):
        if self._frames:
            return self._frames.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self, code=1000):
        self.closed_with = code
        self.close_codes.append(code)


class HoldClient(FakeClient):
    """receive() 永不返回，模拟客户端一直连着，让泵结果（而非客户端断开）决定会话走向。"""

    async def receive(self):
        await asyncio.Event().wait()


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


class BoomEngineConn(FakeEngineConn):
    """首条引擎消息即抛 PayloadTooBig（模拟引擎消息超 ENGINE_MAX_MSG_BYTES）。"""

    def __aiter__(self):
        return self._boom()

    async def _boom(self):
        raise websockets.PayloadTooBig(5 * 1024 * 1024, 4 * 1024 * 1024)
        yield  # pragma: no cover - 让本函数成为 async generator


class ScriptEngineConn(FakeEngineConn):
    """按脚本逐条吐出引擎消息（str）后 EOF。"""

    def __init__(self, messages):
        super().__init__()
        self._messages = messages

    def __aiter__(self):
        return self._script()

    async def _script(self):
        for m in self._messages:
            yield m


class HangEngineConn(FakeEngineConn):
    """引擎永不回消息：async for 挂起，模拟空闲期间引擎静默。"""

    def __aiter__(self):
        return self._hang()

    async def _hang(self):
        await asyncio.Event().wait()
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


# --- 句级 is_final 语义：多句会话不下线 ------------------------------------------------

def test_engine_is_final_message_does_not_end_session():
    # FunASR vLLM 实时协议中 VAD 每切一句结果帧都带 is_final:true —— 它只是
    # 该句定稿信号，不是会话结束；适配层只下推 final 帧，泵必须继续跑
    client = FakeClient({"type": "start"})
    sentences = []
    end = run(backend._forward_engine_message(
        client, json.dumps({"sentences": [{"text": "第一句"}], "is_final": True}),
        sentences))
    assert end is False
    assert client.sent == [{"type": "final", "text": "第一句"}]


def test_engine_stopped_event_ends_session():
    client = FakeClient({"type": "start"})
    end = run(backend._forward_engine_message(
        client, json.dumps({"event": "stopped"}), []))
    assert end is True


def test_multi_sentence_session_stays_online(monkeypatch):
    # P1 回归：一句带 is_final 后连接必须保持，第二句的 final 照常下发，
    # 直到引擎 stopped 事件（客户端 stop 的确认）才结束会话
    conn = ScriptEngineConn([
        json.dumps({"sentences": [{"text": "你好"}], "is_final": True}),
        json.dumps({"sentences": [{"text": "你好"}, {"text": "世界"}], "is_final": True}),
        json.dumps({"event": "stopped"}),
    ])
    monkeypatch.setattr(backend.websockets, "connect", lambda url, **kw: conn)
    client = HoldClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    finals = [f for f in client.sent if f.get("type") == "final"]
    assert [f["text"] for f in finals] == ["你好", "世界"]
    assert client.closed_with == 1000  # stopped 后正常关闭，非中途断连


# --- WS 背压 ----------------------------------------------------------------------

def test_engine_connect_inbound_max_size_capped(monkeypatch):
    # 引擎每条消息带全量 sentences 历史，connect 必须带 max_size 上限（4MB），
    # 不能再是 None（放任单条消息撑爆内存）
    captured = {}
    conn = FakeEngineConn()

    def fake_connect(url, **kw):
        captured.update(kw)
        return conn

    monkeypatch.setattr(backend.websockets, "connect", fake_connect)
    client = FakeClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert captured["max_size"] == 4 * 1024 * 1024


def test_oversized_audio_frame_rejected_error_and_close_1009(fake_engine):
    # 单帧 >1MB：error 帧 + close 1009 (message too big)，帧不进引擎
    big = b"\x00" * (1024 * 1024 + 1)
    client = FakeClient({"type": "start", "language": "ja"},
                        frames=[{"type": "websocket.receive", "bytes": big}])
    run(backend.handle_realtime(client, "ws://fake"))
    assert 1009 in client.close_codes
    errors = [f for f in client.sent if f.get("type") == "error"]
    assert errors and "单帧" in errors[0]["message"]
    assert not [c for c in fake_engine.commands if isinstance(c, bytes)]


def test_audio_frame_at_size_limit_forwarded(fake_engine):
    # 恰好等于上限的单帧照常转发（边界不误伤）
    frame = b"\x00" * (1024 * 1024)
    client = FakeClient({"type": "start", "language": "ja"},
                        frames=[{"type": "websocket.receive", "bytes": frame}])
    run(backend.handle_realtime(client, "ws://fake"))
    assert not [f for f in client.sent if f.get("type") == "error"]
    assert frame in fake_engine.commands


def test_engine_oversized_message_surfaces_error_frame(monkeypatch):
    # 引擎消息超 max_size → websockets 抛 PayloadTooBig（WebSocketException 子类），
    # 现有异常路径兜住后客户端收到通用 error 帧
    monkeypatch.setattr(backend.websockets, "connect", lambda url, **kw: BoomEngineConn())
    client = HoldClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    errors = [f for f in client.sent if f.get("type") == "error"]
    assert errors and "引擎连接异常" in errors[0]["message"]


# --- error 帧 code 字段（X-6 协议：message 为中文兜底，客户端按 code 本地化） --------

def error_frames(client):
    return [f for f in client.sent if f.get("type") == "error"]


def assert_error_code(client, expected):
    """每个 error 帧都必须携带契约枚举内的 code，且首个帧为期望值。"""
    errors = error_frames(client)
    assert errors, "未收到 error 帧"
    for f in errors:
        assert f.get("code") in backend.ERROR_CODES, f"非法 code: {f!r}"
        assert f.get("message"), "message 兜底文案不得为空"
    assert errors[0]["code"] == expected


def test_error_code_enum_matches_contract():
    # X-6 协议契约：与 LLM/Mascot 门面同一枚举集合，勿自行增删
    assert backend.ERROR_CODES == frozenset({
        "engine_error", "engine_conn_failed", "idle_timeout", "protocol_error",
        "frame_too_large", "audio_limit_exceeded", "internal_error",
    })


def test_error_code_first_frame_must_be_start():
    client = FakeClient({"type": "stop"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "protocol_error")


def test_error_code_sample_rate_non_integer():
    client = FakeClient({"type": "start", "language": "ja", "sample_rate": "abc"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "protocol_error")


def test_error_code_sample_rate_not_16k():
    client = FakeClient({"type": "start", "language": "ja", "sample_rate": 8000})
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "protocol_error")


def test_error_code_language_not_whitelisted():
    client = FakeClient({"type": "start", "language": "ko", "sample_rate": 16000})
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "protocol_error")


def test_error_code_engine_error_event(monkeypatch):
    conn = ScriptEngineConn([json.dumps({"event": "error", "error": "boom"})])
    monkeypatch.setattr(backend.websockets, "connect", lambda url, **kw: conn)
    client = HoldClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "engine_error")


def test_error_code_idle_timeout(monkeypatch):
    monkeypatch.setattr(backend, "IDLE_TIMEOUT", 0.05)
    monkeypatch.setattr(backend, "DRAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(backend.websockets, "connect", lambda url, **kw: HangEngineConn())
    client = HoldClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "idle_timeout")


def test_error_code_frame_too_large(fake_engine):
    big = b"\x00" * (1024 * 1024 + 1)
    client = FakeClient({"type": "start", "language": "ja"},
                        frames=[{"type": "websocket.receive", "bytes": big}])
    run(backend.handle_realtime(client, "ws://fake"))
    assert 1009 in client.close_codes
    assert_error_code(client, "frame_too_large")


def test_error_code_audio_limit_exceeded(fake_engine, monkeypatch):
    monkeypatch.setattr(backend, "MAX_AUDIO_BYTES", 100)
    client = FakeClient({"type": "start", "language": "ja"},
                        frames=[{"type": "websocket.receive", "bytes": b"\x00" * 60},
                                {"type": "websocket.receive", "bytes": b"\x00" * 60}])
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "audio_limit_exceeded")


def test_error_code_engine_oversized_message(monkeypatch):
    monkeypatch.setattr(backend.websockets, "connect", lambda url, **kw: BoomEngineConn())
    client = HoldClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert_error_code(client, "engine_error")


def test_error_code_engine_connect_failed(monkeypatch):
    def boom(url, **kw):
        raise OSError("connection refused")
    monkeypatch.setattr(backend.websockets, "connect", boom)
    client = FakeClient({"type": "start", "language": "ja"})
    run(backend.handle_realtime(client, "ws://fake"))
    assert client.closed_with == 1011
    assert_error_code(client, "engine_conn_failed")


def test_error_code_qwen3_stub():
    import backend_qwen3
    client = FakeClient({"type": "start"})
    run(backend_qwen3.handle_realtime(client, "ws://fake"))
    assert client.closed_with == 1008
    assert_error_code(client, "internal_error")
