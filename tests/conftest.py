"""pytest 公共夹具：导入 tools/server.py 前注入测试用环境变量。

server.py 在模块级读取 CHII_ASR_* 配置（未设置 CHII_ASR_API_KEY 会 sys.exit），
故环境变量必须在 import 之前就绪。引擎依赖不启动：端点测试用 monkeypatch
替换 server._engine（httpx.AsyncClient）与 backend.handle_realtime。
"""
import base64
import hashlib
import hmac
import os
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

os.environ.setdefault("CHII_ASR_API_KEY", "test-asr-key")
os.environ.setdefault("CHII_ASR_TICKET_SECRET", "test-ticket-secret")

import server  # noqa: E402

API_KEY = os.environ["CHII_ASR_API_KEY"]
AUTH = {"Authorization": "Bearer " + API_KEY}


def make_ticket(identity=None, exp=None, jti="jti-1", secret=None):
    """按 server._ticket_consume 的验签格式铸造票据（默认新版 4 段）。"""
    secret = server.TICKET_SECRET if secret is None else secret
    exp = int(time.time()) + 60 if exp is None else exp
    idb64 = ""
    if identity is not None:
        idb64 = base64.urlsafe_b64encode(identity.encode()).decode().rstrip("=")
    signed = "asr-realtime.%d.%s" % (exp, jti) + (("." + idb64) if idb64 else "")
    sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()[:32]
    if idb64:
        return "%d.%s.%s.%s" % (exp, jti, idb64, sig)
    return "%d.%s.%s" % (exp, jti, sig)


@pytest.fixture(autouse=True)
def reset_global_state():
    """每个测试隔离模块级全局状态（限流桶 / 票据核销集合 / WS 并发桶）。"""
    server._hits.clear()
    server._used_tickets.clear()
    server._ws_active.clear()
    server._ws_active_total = 0
    yield
    server._hits.clear()
    server._used_tickets.clear()
    server._ws_active.clear()
    server._ws_active_total = 0
