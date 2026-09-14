#!/usr/bin/env python3
"""小叽 ASR 服务门面: API Key 鉴权 + 每 IP 限流 + OpenAI 兼容垫片 + 统一流式 WS 网关。

架构与家族其他服务一致: 推理引擎跑在 Docker 容器内 (见 docker/), 本脚本跑在宿主机,
对客户端屏蔽引擎细节。后端经 CHII_ASR_BACKEND 切换 (funasr | qwen3),
HTTP 批量转写两侧均原生兼容 OpenAI 协议, 切换零改动; 流式协议差异由
tools/backend_funasr.py / tools/backend_qwen3.py 吸收。

对外端点 (客户端 baseUrl 填 http(s)://<IP>:9881/v1):
    GET  /healthz                  → 200 (免鉴权探活)
    GET  /v1/models                → 固定返回 chii-asr
    POST /v1/audio/transcriptions  → OpenAI 批量转写, 原样透传引擎 (multipart 文件上传)
    WS   /v1/realtime              → 统一流式协议 (start/音频帧/stop → partial/final,
                                     详见 tools/backend_funasr.py docstring)

配置走环境变量 (生产环境经 /etc/chobits-chii-asr.env 注入, 见 docs/deployment.md):
    CHII_ASR_API_KEY          客户端 Bearer 鉴权 key (必填, 未设置拒绝启动)
    CHII_ASR_BACKEND          引擎后端: funasr (默认) | qwen3
    CHII_ASR_ENGINE_HTTP_URL  引擎 HTTP 地址, 默认 http://127.0.0.1:9001
    CHII_ASR_ENGINE_WS_URL    引擎 WS 地址, 默认 ws://127.0.0.1:10095
    CHII_ASR_ENGINE_MODEL     引擎注册的模型名 (funasr --model-path 模式为 custom), 默认 custom
    CHII_ASR_RATE_LIMIT       每 IP 每分钟限流次数 (转写与流式), 默认 60, 设 0 关闭
    CHII_ASR_MAX_UPLOAD_BYTES 批量转写上传上限 (字节), 默认 25MB
    CHII_ASR_WS_MAX_PER_IP    流式每 IP 并发连接上限, 默认 4
    CHII_ASR_WS_MAX_GLOBAL    流式全局并发连接上限, 默认 32
    CHII_ASR_WS_SESSION_MAX   流式会话最长秒数, 默认 300
    CHII_ASR_WS_IDLE_SECONDS  流式空闲超时秒数 (无帧即断), 默认 60
    CHII_ASR_WS_MAX_AUDIO_BYTES 流式单会话音频总量上限 (字节), 默认 10MB
    CHII_ASR_SSL_CERTFILE / CHII_ASR_SSL_KEYFILE  同时设置则以 HTTPS/WSS 启动

用法:
    python3 tools/server.py [端口, 默认 9881]

注意: 未启用 TLS 时 API Key 明文传输, 仅适合内网/低风险公网场景;
面向公众分发应用时, 建议由后端服务代为调用, 不要把唯一密钥嵌进客户端。
"""

import asyncio
import hashlib
import hmac
import os
import re
import sys
import time
from collections import defaultdict, deque


def _getenv(suffix: str, default: str = "") -> str:
    """读取 CHII_ASR_<suffix> 环境变量。"""
    return os.environ.get(f"CHII_ASR_{suffix}", default)


API_KEY = _getenv("API_KEY")
if not API_KEY:
    sys.exit("[错误] 未设置 CHII_ASR_API_KEY 环境变量, 拒绝以无鉴权方式启动")

# 与签发服务 (newapi_provision.py) 共享的 WS 票据签名密钥：
# app 先经垫片 /v1/asr/ticket 换短时票据，再用票据直连 /v1/realtime，
# 门面本地验签即可，无需回调 New API
TICKET_SECRET = _getenv("TICKET_SECRET")
BIND = _getenv("BIND", "127.0.0.1")  # 默认只绑本机（生产由 Caddy 反代）；显式改绑公网见下方 TLS 守卫


def _ticket_consume(ticket: str) -> bool:
    """校验并核销短时票据 "<expiry>.<jti>.<hmac16>"：HMAC-SHA256 签名 + 60 秒有效
    + 随机 jti 单次使用（验过即作废，防 60s 窗口内重放/一票多开）。"""
    if not TICKET_SECRET:
        return False
    parts = ticket.split(".")
    if len(parts) != 3:
        return False
    exp_str, jti, sig = parts
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    now = int(time.time())
    if exp <= now:
        return False
    if not jti or len(jti) > 64:
        return False
    expected = hmac.new(TICKET_SECRET.encode(),
                        f"asr-realtime.{exp}.{jti}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return False
    # 核销：顺手清掉过期项，再查重
    for used_jti in [k for k, used_exp in _used_tickets.items() if used_exp <= now]:
        del _used_tickets[used_jti]
    if jti in _used_tickets:
        return False
    _used_tickets[jti] = exp
    return True

RATE_LIMIT = int(_getenv("RATE_LIMIT", "60"))
MAX_UPLOAD_BYTES = int(_getenv("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
WS_MAX_PER_IP = int(_getenv("WS_MAX_PER_IP", "4"))
WS_MAX_GLOBAL = int(_getenv("WS_MAX_GLOBAL", "32"))
WS_SESSION_MAX = float(_getenv("WS_SESSION_MAX", "300"))
BACKEND = _getenv("BACKEND", "funasr")
ENGINE_HTTP_URL = _getenv("ENGINE_HTTP_URL", "http://127.0.0.1:9001").rstrip("/")
ENGINE_WS_URL = _getenv("ENGINE_WS_URL", "ws://127.0.0.1:10095")
# 引擎侧注册的模型名: funasr-server 按加载方式注册 (--model-path → "custom", --model → 别名),
# 不认门面对外名 chii-asr, 透传批量转写时改写 model 字段为该值
ENGINE_MODEL = _getenv("ENGINE_MODEL", "custom")

# TLS: 两个变量都设置时以 HTTPS/WSS 启动 (自签名证书见 docs/deployment.md)
SSL_CERTFILE = _getenv("SSL_CERTFILE")
SSL_KEYFILE = _getenv("SSL_KEYFILE")
if bool(SSL_CERTFILE) != bool(SSL_KEYFILE):
    sys.exit("[错误] CHII_ASR_SSL_CERTFILE 与 CHII_ASR_SSL_KEYFILE 必须同时设置")
# 明文 HTTP 绑非回环地址 = Bearer key 明文过网：拒绝启动（生产应由 Caddy 终结 TLS，
# 门面绑回环；确需内网明文直连时请自行评估后再改 BIND）
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
if BIND not in _LOOPBACK and not SSL_CERTFILE:
    sys.exit(f"[错误] BIND={BIND} 为非回环地址但未配置 TLS（CHII_ASR_SSL_CERTFILE/KEYFILE），"
             "明文 HTTP 会泄露 API Key，拒绝启动")

if BACKEND == "funasr":
    import backend_funasr as backend
elif BACKEND == "qwen3":
    import backend_qwen3 as backend
else:
    sys.exit(f"[错误] 未知后端 CHII_ASR_BACKEND={BACKEND!r} (可选: funasr | qwen3)")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request, WebSocket  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
from starlette.datastructures import UploadFile  # noqa: E402

ASR_MODEL = "chii-asr"

app = FastAPI(title="chobits-chii-asr", docs_url=None, redoc_url=None)
_engine = httpx.AsyncClient(base_url=ENGINE_HTTP_URL, timeout=httpx.Timeout(300.0))
_hits: dict[str, deque] = defaultdict(deque)
_used_tickets: dict[str, int] = {}  # jti -> exp，核销集合（过期即清）
_ws_active: dict[str, int] = defaultdict(int)  # ip -> 在途流式连接数
_ws_active_total = 0


def _client_ip(host: str, headers) -> str:
    """真实客户端 IP：对端为本机（Caddy/LLM 垫片）时采信 X-Forwarded-For
    最后一跳（反代把真实 IP 追加在尾部，取第一跳会被客户端伪造绕过——
    且 Caddy 侧已用 header_up 覆盖伪造值）；直连不采信 XFF。"""
    if host == "127.0.0.1":
        xff = headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return host


def _key_ok(provided: str) -> bool:
    """常量时间比较 API key（远程时序攻击难利用，但修复零成本）。"""
    if not provided:
        return False
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(API_KEY.encode()).digest())


def _authorized(request: Request) -> bool:
    # 只认 Authorization 头；不再接受 ?api_key=（query string 会进访问日志）
    auth = request.headers.get("Authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    return _key_ok(auth[7:].strip())


def _rate_ok(ip: str) -> bool:
    """滑动窗口限流: 每 IP 每分钟 RATE_LIMIT 次, 0 关闭。"""
    if RATE_LIMIT <= 0:
        return True
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if not q:
        _hits.pop(ip, None)  # 过期清空即删键，防海量 IP 键只增不清
        q = _hits[ip]
    if len(q) >= RATE_LIMIT:
        return False
    q.append(now)
    return True


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "model": ASR_MODEL, "backend": BACKEND}


@app.get("/v1/models")
async def models(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"object": "list", "data": [
        {"id": ASR_MODEL, "object": "model", "created": 0, "owned_by": "chobits-chii"},
    ]}


# 转写透传的表单字段白名单（其余字段一律丢弃，防引擎特有参数被滥用）
TRANSCRIPTION_ALLOWED_FIELDS = {"model", "language", "prompt", "response_format",
                                "temperature", "timestamp_granularities[]"}
# 引擎响应头只回 Content-Type（Content-Length 由 Response 自算），不泄露引擎指纹
RESPONSE_HEADER_ALLOWLIST = {"content-type"}


def _sanitize_filename(name: str) -> str:
    """上传文件名净化：basename + 字符白名单，防容器内路径穿越/控制字符。"""
    base = os.path.basename(name or "")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    return cleaned or "audio.bin"


@app.post("/v1/audio/transcriptions")
async def transcriptions(request: Request):
    """OpenAI 批量转写垫片: 透传 multipart 表单, 仅把 model 字段改写为引擎注册名。

    引擎只认自己注册的模型名 (见 ENGINE_MODEL), 对外统一暴露 chii-asr;
    文件与表单字段白名单 (language/prompt 等) 转发, 响应原样回传。"""
    # 限流前置：鉴权失败也计桶，在线爆破与未鉴权消耗都有成本
    if not _rate_ok(_client_ip(request.client.host if request.client else "unknown",
                               request.headers)):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # 上传大小上限：Content-Length 预检 + 读入累计兜底（ chunked 可不带长度；
    # 不设限时整文件会双份驻留门面内存，并发大文件即内存/CPU DoS）
    length = request.headers.get("content-length")
    if length and int(length) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)
    data: dict = {}
    files: dict = {}
    total = 0
    for key, value in (await request.form()).multi_items():
        if key == "model":
            data[key] = ENGINE_MODEL
        elif isinstance(value, UploadFile):
            content = await value.read()
            total += len(content)
            if total > MAX_UPLOAD_BYTES:
                return JSONResponse({"error": "file too large"}, status_code=413)
            files[key] = (_sanitize_filename(value.filename), content, value.content_type)
        elif key in TRANSCRIPTION_ALLOWED_FIELDS:
            data[key] = value
    if "model" not in data:
        data["model"] = ENGINE_MODEL
    try:
        resp = await _engine.post("/v1/audio/transcriptions", data=data, files=files)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"upstream error: {type(e).__name__}"},
                            status_code=502)
    headers = {k: v for k, v in resp.headers.items() if k.lower() in RESPONSE_HEADER_ALLOWLIST}
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    """统一流式识别入口: 鉴权/限流/并发准入后交给当前后端的适配层。

    资源防护（引擎流式会话独占 GPU，不设防时挂死连接即可拖垮全服务）：
    每 IP / 全局并发上限、会话最长 WS_SESSION_MAX 秒；空闲超时与音频总量
    上限在适配层逐帧执行（backend_funasr）。"""
    # 限流前置（失败也计桶）；票据核销放在限流之后，避免被限流时白白烧掉一次性票据
    ip = _client_ip(ws.client.host if ws.client else "unknown", ws.headers)
    if not _rate_ok(ip):
        await ws.close(code=4429)
        return
    # WS 只认 Authorization 头与一次性票据；不再接受 ?api_key=（会进访问日志）
    auth = ws.headers.get("Authorization", "")
    if not (auth.lower().startswith("bearer ") and _key_ok(auth[7:].strip())) and \
            not _ticket_consume(ws.query_params.get("ticket", "")):
        await ws.close(code=4401)
        return
    global _ws_active_total
    if _ws_active_total >= WS_MAX_GLOBAL or _ws_active[ip] >= WS_MAX_PER_IP:
        await ws.close(code=4429)
        return
    _ws_active[ip] += 1
    _ws_active_total += 1
    await ws.accept()
    try:
        await asyncio.wait_for(backend.handle_realtime(ws, ENGINE_WS_URL),
                               timeout=WS_SESSION_MAX)
    except Exception as e:  # 适配层异常/会话超时都不暴露内部细节
        sys.stderr.write(f"[asr] realtime aborted: {type(e).__name__}: {e}\n")
        try:
            await ws.close(code=1011)
        except Exception:
            pass
    finally:
        _ws_active_total -= 1
        if _ws_active[ip] <= 1:
            _ws_active.pop(ip, None)
        else:
            _ws_active[ip] -= 1


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(_getenv("PORT", "9881"))
    scheme = "https" if SSL_CERTFILE else "http"
    print(f"[asr] {scheme}://{BIND}:{port}  backend={BACKEND}"
          f"  /v1/models /v1/audio/transcriptions /v1/realtime"
          f" → {ENGINE_HTTP_URL} / {ENGINE_WS_URL}", file=sys.stderr)
    uvicorn.run(app, host=BIND, port=port,
                ssl_certfile=SSL_CERTFILE or None, ssl_keyfile=SSL_KEYFILE or None,
                limit_concurrency=128, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
