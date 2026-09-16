"""tools/server.py 端点级测试：TestClient + 内存假引擎（不触真实 :9001/:10095）。"""
import asyncio
from contextlib import ExitStack

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server
from conftest import AUTH, make_ticket


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


# --- 健康检查与鉴权 ---------------------------------------------------------------

def test_healthz_no_auth_required(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_models_no_key_401(client):
    assert client.get("/v1/models").status_code == 401


def test_models_with_key_200(client):
    resp = client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "chii-asr"


# --- 批量转写：引擎错误通用化 --------------------------------------------------------

class FakeResp:
    def __init__(self, status_code, content=b"{}", content_type="application/json"):
        self.status_code = status_code
        self.content = content
        # x-engine-* 是引擎指纹，白名单外不应回传给客户端
        self.headers = {"content-type": content_type, "x-engine-version": "nano-1.0"}


class FakeEngine:
    def __init__(self, resp=None, exc=None):
        self.resp = resp
        self.exc = exc
        self.calls = []

    async def post(self, url, data=None, files=None):
        self.calls.append({"url": url, "data": data, "files": files})
        if self.exc is not None:
            raise self.exc
        return self.resp


def _upload(client, headers=AUTH):
    return client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", b"RIFF-fake", "audio/wav")},
        data={"model": "chii-asr", "language": "ja"},
        headers=headers)


def test_transcription_engine_5xx_becomes_generic_502(client, monkeypatch):
    engine = FakeEngine(resp=FakeResp(500, b"Internal: /app/engine/secret.py traceback"))
    monkeypatch.setattr(server, "_engine", engine)
    resp = _upload(client)
    assert resp.status_code == 502
    assert resp.json() == {"error": "asr engine error"}
    assert "secret" not in resp.text  # 引擎内部细节不外泄


def test_transcription_engine_4xx_generic_message(client, monkeypatch):
    engine = FakeEngine(resp=FakeResp(400, b"bad audio: /container/path detail"))
    monkeypatch.setattr(server, "_engine", engine)
    resp = _upload(client)
    assert resp.status_code == 400  # 保留状态码语义
    assert resp.json() == {"error": "transcription request rejected"}
    assert "container" not in resp.text


def test_transcription_engine_unreachable_502(client, monkeypatch):
    engine = FakeEngine(exc=httpx.ConnectError("refused"))
    monkeypatch.setattr(server, "_engine", engine)
    resp = _upload(client)
    assert resp.status_code == 502
    assert "upstream error" in resp.json()["error"]


def test_transcription_success_passthrough(client, monkeypatch):
    engine = FakeEngine(resp=FakeResp(200, b'{"text": "hello"}'))
    monkeypatch.setattr(server, "_engine", engine)
    resp = _upload(client)
    assert resp.status_code == 200
    assert resp.content == b'{"text": "hello"}'
    assert "x-engine-version" not in resp.headers  # 响应头白名单
    # 对外模型名改写为引擎注册名；白名单字段转发
    call = engine.calls[0]
    assert call["data"]["model"] == server.ENGINE_MODEL
    assert call["data"]["language"] == "ja"


def test_transcription_no_key_401(client):
    assert _upload(client, headers={}).status_code == 401


# --- 流式 WS：并发准入 -------------------------------------------------------------

async def _hang(ws, url):
    await asyncio.sleep(30)


@pytest.fixture
def hold_engine(monkeypatch):
    """后端适配层替身：连接挂起，占用并发槽位直到测试结束。"""
    monkeypatch.setattr(server.backend, "handle_realtime", _hang)


def _open(stack, client, **kwargs):
    return stack.enter_context(client.websocket_connect("/v1/realtime", **kwargs))


def _expect_close(client, code, **kwargs):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/v1/realtime", **kwargs):
            pass
    assert exc.value.code == code


def test_ws_no_auth_rejected_4401(client, hold_engine):
    _expect_close(client, 4401)


def test_ws_per_client_limit_by_identity(client, hold_engine):
    # 同一票据身份第 5 个连接被拒（WS_MAX_PER_CLIENT=4）
    tickets = [make_ticket(identity="acct:u1", jti="jti-id-%d" % i) for i in range(5)]
    with ExitStack() as stack:
        for ticket in tickets[:4]:
            _open(stack, client, params={"ticket": ticket})
        _expect_close(client, 4429, params={"ticket": tickets[4]})
    # 超限拒绝 (4429) 不得消耗一次性票据：被拒的第 5 张票未被核销
    assert "jti-id-4" not in server._used_tickets


def test_ws_over_limit_rejected_ticket_still_usable(client, hold_engine):
    # 4429 拒绝不烧票：槽位释放后同一张票可正常入场并被核销
    tickets = [make_ticket(identity="acct:u1", jti="jti-save-%d" % i) for i in range(5)]
    with ExitStack() as stack:
        for ticket in tickets[:4]:
            _open(stack, client, params={"ticket": ticket})
        _expect_close(client, 4429, params={"ticket": tickets[4]})
    # 4 个连接随 ExitStack 关闭、槽位释放；同一张第 5 票此刻应能准入
    with client.websocket_connect("/v1/realtime", params={"ticket": tickets[4]}):
        pass
    assert "jti-save-4" in server._used_tickets  # accept 后才真正核销


def test_ws_accept_consumes_ticket(client, hold_engine):
    # 正常入场 (accept) 后票据被核销：同一张票二次使用按重放拒绝 (4401)
    ticket = make_ticket(identity="acct:u1", jti="jti-consume")
    with client.websocket_connect("/v1/realtime", params={"ticket": ticket}):
        assert "jti-consume" in server._used_tickets
    _expect_close(client, 4401, params={"ticket": ticket})


def test_ws_distinct_identities_independent_quotas(client, hold_engine):
    with ExitStack() as stack:
        for i in range(4):
            _open(stack, client,
                  params={"ticket": make_ticket(identity="acct:u1", jti="jti-a%d" % i)})
        # 另一个身份不受 acct:u1 的配额影响
        _open(stack, client,
              params={"ticket": make_ticket(identity="acct:u2", jti="jti-b0")})


def test_ws_no_identity_falls_back_to_per_ip(client, hold_engine):
    # API key 直连无身份 → 按 IP 计数，同样第 5 个被拒
    with ExitStack() as stack:
        for _ in range(4):
            _open(stack, client, headers=AUTH)
        _expect_close(client, 4429, headers=AUTH)


def test_ws_slot_released_after_disconnect(client, hold_engine):
    with client.websocket_connect("/v1/realtime", headers=AUTH):
        pass
    # 断开后槽位释放，同 IP 可以再开满 4 个
    with ExitStack() as stack:
        for _ in range(4):
            _open(stack, client, headers=AUTH)
