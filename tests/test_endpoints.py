"""tools/server.py 端点级测试：TestClient + 内存假引擎（不触真实 :9001/:10095）。"""
import asyncio
import hashlib
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server
from conftest import AUTH, API_KEY, make_ticket


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


# --- /healthz/deep 深度健康检查 ------------------------------------------------

def test_healthz_deep_no_key_401(client, monkeypatch):
    # 深探测是真实转写（有引擎成本），与其余端点一样须带 key：无 key/错 key 都 401 且不触引擎
    engine = FakeEngine(resp=FakeResp(200, b'{"text": ""}'))
    monkeypatch.setattr(server, "_engine", engine)
    assert client.get("/healthz/deep").status_code == 401
    assert client.get("/healthz/deep",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert engine.calls == []


def test_healthz_deep_ok_200(client, monkeypatch):
    engine = FakeEngine(resp=FakeResp(200, b'{"text": ""}'))  # 静音 → 空串属正常应答
    monkeypatch.setattr(server, "_engine", engine)
    resp = client.get("/healthz/deep", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["status"] == "ok"
    assert body["backend"] == server.BACKEND  # 引擎标识字段
    # 探测走与批量转写相同的引擎路径：同端点 + 引擎注册模型名 + 内置 wav
    (call,) = engine.calls
    assert call["url"] == "/v1/audio/transcriptions"
    assert call["data"] == {"model": server.ENGINE_MODEL}
    name, content, ctype = call["files"]["file"]
    assert name == "healthz_probe.wav" and ctype == "audio/wav"
    assert content == server.PROBE_WAV


def test_healthz_deep_engine_5xx_503(client, monkeypatch):
    engine = FakeEngine(resp=FakeResp(500, b"Internal: /app/engine/secret.py traceback"))
    monkeypatch.setattr(server, "_engine", engine)
    resp = client.get("/healthz/deep", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["ok"] is False
    assert "secret" not in resp.text  # 引擎内部细节不外泄


def test_healthz_deep_engine_unreachable_503(client, monkeypatch):
    engine = FakeEngine(exc=httpx.ConnectError("refused"))
    monkeypatch.setattr(server, "_engine", engine)
    resp = client.get("/healthz/deep", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["engine"] == "unreachable: ConnectError"


def test_healthz_deep_bad_response_503(client, monkeypatch):
    # 引擎 200 但回的不是转写 JSON（变砖/代理错乱）：判 degraded 而非 200
    engine = FakeEngine(resp=FakeResp(200, b"<html>proxy error</html>",
                                      content_type="text/html"))
    monkeypatch.setattr(server, "_engine", engine)
    resp = client.get("/healthz/deep", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["engine"] == "bad response"


def test_healthz_deep_ttl_cache(client, monkeypatch):
    engine = FakeEngine(resp=FakeResp(200, b'{"text": ""}'))
    monkeypatch.setattr(server, "_engine", engine)
    assert client.get("/healthz/deep", headers=AUTH).status_code == 200
    assert client.get("/healthz/deep", headers=AUTH).status_code == 200
    assert len(engine.calls) == 1  # TTL 内缓存生效，不重复烧引擎


class _LockedLock:
    """已上锁的锁替身：locked() 恒真（探测在途场景，端点走缓存/503 分支，不会再获取）。"""

    def locked(self):
        return True


def test_healthz_deep_probe_in_progress_no_cache_503(client, monkeypatch):
    # 服务刚启动（无缓存）时探测在途：快速 503 probe in progress，不排队等引擎
    monkeypatch.setattr(server, "_deep_probe_lock", _LockedLock())
    resp = client.get("/healthz/deep", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["engine"] == "probe in progress"


def test_healthz_deep_probe_in_progress_serves_stale_cache(client, monkeypatch):
    # 已有缓存（哪怕已过期）时探测在途：直接吃缓存，不在探测窗口内对监控抖 503
    monkeypatch.setattr(server, "_deep_probe_lock", _LockedLock())
    server._deep_probe.update({"at": time.time() - 3600, "ok": True, "engine": "ok"})
    resp = client.get("/healthz/deep", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_deep_probe_does_not_queue_behind_real_transcription(client, monkeypatch):
    """引擎被一个真实批量转写占住（挂闸门）时：/healthz/deep 按自身短超时快速 503
    （探测不排在真实请求后面等引擎），且闸门放行后真实转写照常 200。"""
    gate = threading.Event()

    class BusyEngine:
        def __init__(self):
            self.probe_calls = 0
            self.transcription_calls = 0

        async def post(self, url, data=None, files=None, timeout=None):
            # 探测请求：引擎忙到探测超时（等价真实引擎排队超过探测超时）
            if files and files.get("file", ("",))[0] == "healthz_probe.wav":
                self.probe_calls += 1
                await asyncio.sleep(float(timeout) if timeout else 30)
                raise httpx.ReadTimeout("engine busy beyond probe timeout")
            # 真实批量转写：挂闸门占住引擎
            self.transcription_calls += 1
            while not gate.is_set():
                await asyncio.sleep(0.02)
            return FakeResp(200, b'{"text": "real transcript"}')

    engine = BusyEngine()
    monkeypatch.setattr(server, "_engine", engine)
    monkeypatch.setattr(server, "DEEP_PROBE_TIMEOUT", 0.3)

    result = {}

    def upload():
        with TestClient(server.app) as c2:
            result["resp"] = _upload(c2)

    worker = threading.Thread(target=upload)
    worker.start()
    time.sleep(0.2)  # 让真实转写先占住引擎
    t0 = time.monotonic()
    resp = client.get("/healthz/deep", headers=AUTH)
    elapsed = time.monotonic() - t0
    assert resp.status_code == 503  # 引擎忙：探测超时 → degraded
    assert elapsed < 5.0  # 快速失败，没有排在真实请求后面
    assert engine.probe_calls == 1 and engine.transcription_calls == 1

    gate.set()
    worker.join(10)
    assert result["resp"].status_code == 200  # 真实流量未被探测饿死/干扰
    assert result["resp"].json()["text"] == "real transcript"


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

    async def post(self, url, data=None, files=None, timeout=None):
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


def test_transcription_audit_log_line(client, monkeypatch, capsys):
    # 审计行格式与 TTS 门面中间件一致（key 哈希 + IP + 请求体长度），不记明文 key；
    # 鉴权失败的请求不记（与 TTS 中间件"验过 key 才记"对齐）
    engine = FakeEngine(resp=FakeResp(200, b'{"text": "hello"}'))
    monkeypatch.setattr(server, "_engine", engine)
    _upload(client, headers={})  # 401 无审计行
    assert _upload(client).status_code == 200
    digest = hashlib.sha256(API_KEY.encode()).hexdigest()[:12]
    lines = [l for l in capsys.readouterr().err.splitlines() if l.startswith("[audit] ")]
    assert len(lines) == 1
    (line,) = lines
    assert line.startswith(f"[audit] /v1/audio/transcriptions key={digest} ip=testclient len=")
    assert API_KEY not in line  # 明文 key 不落日志


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


def test_ws_non_ascii_ticket_sig_rejected_4401_not_500(client, hold_engine):
    # 非 ASCII 签名不得触发握手 500（旧实现 compare_digest TypeError 透出栈）；
    # 形态前置校验后按验签失败关闭 4401
    ticket = make_ticket(identity="acct:u1")
    parts = ticket.split(".")
    parts[-1] = "签" * 16
    _expect_close(client, 4401, params={"ticket": ".".join(parts)})


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


# --- 流式 WS：accept 失败路径（弱网握手途中断连） -------------------------------------

class AcceptBoomWS:
    """starlette WebSocket 替身：accept() 抛异常（模拟握手途中客户端已断连，
    starlette 报 "Cannot call accept once disconnect message has been received"）。"""

    def __init__(self, ticket):
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers = {}
        self.query_params = {"ticket": ticket}
        self.closed = []

    async def accept(self):
        raise RuntimeError(
            "Cannot call accept once disconnect message has been received")

    async def close(self, code=1000):
        self.closed.append(code)


def test_ws_accept_failure_does_not_leak_counter_or_burn_ticket():
    # P0 回归：accept 抛异常时并发计数必须配平（旧实现自增在前 → 永久泄漏，
    # 同身份泄漏 4 次后被永久 4429），且一次性票据不得被烧毁
    ticket = make_ticket(identity="acct:u1", jti="jti-boom")
    ws = AcceptBoomWS(ticket)
    asyncio.run(server.realtime(ws))
    assert server._ws_active_total == 0
    assert dict(server._ws_active) == {}
    assert "jti-boom" not in server._used_tickets
    # 票未烧：验签仍可通过，且身份配额未被泄漏的计数占用
    assert server._ticket_verify(ticket)[0] is True


def test_ws_accept_failure_repeated_then_normal_accept_still_admitted():
    # 同一身份连续 accept 失败（弱网重试）不得消耗并发配额，正常连接照常准入
    tickets = [make_ticket(identity="acct:u1", jti="jti-rb-%d" % i) for i in range(3)]
    for t in tickets:
        asyncio.run(server.realtime(AcceptBoomWS(t)))
    assert server._ws_active_total == 0
    ok_identities = [server._ticket_verify(t)[0] for t in tickets]
    assert ok_identities == [True, True, True]
