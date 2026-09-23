#!/usr/bin/env python3
"""小叽 ASR 服务门面: API Key 鉴权 + 每 IP 限流 + OpenAI 兼容垫片 + 统一流式 WS 网关。

架构与家族其他服务一致: 推理引擎跑在 Docker 容器内 (见 docker/), 本脚本跑在宿主机,
对客户端屏蔽引擎细节。后端经 CHII_ASR_BACKEND 切换 (funasr | qwen3),
HTTP 批量转写两侧均原生兼容 OpenAI 协议, 切换零改动; 流式协议差异由
tools/backend_funasr.py / tools/backend_qwen3.py 吸收。

对外端点 (客户端 baseUrl 填 http(s)://<IP>:9881/v1):
    GET  /healthz                  → 200 (免鉴权浅探活)
    GET  /healthz/deep             → 200/503 深度检查 (须 key, 真实转写探测, 见下方健康检查)
    GET  /v1/models                → 固定返回 chii-asr
    POST /v1/audio/transcriptions  → OpenAI 批量转写, 原样透传引擎 (multipart 文件上传)
    WS   /v1/realtime              → 统一流式协议 (start/音频帧/stop → partial/final,
                                     详见 tools/backend_funasr.py docstring)

健康检查:
  - GET /healthz        → 进程级浅探活 (免鉴权, 不触引擎, 不暴露指纹);
  - GET /healthz/deep   → 深度检查: 用一小段内置静音 WAV 走与批量转写相同的引擎 HTTP
    路径发一次真实转写探测 (不经 WS 票据, 直连引擎层), 能发现"进程活着但引擎变砖/
    不可达"的状态; 探测超时 CHII_ASR_DEEP_PROBE_TIMEOUT 秒 (默认 15),
    结果缓存 CHII_ASR_DEEP_PROBE_TTL 秒 (默认 30) 避免监控高频探测烧引擎;
    引擎不可用/超时/回包异常 → 503, 正常 → 200 + ok/backend 字段。
    与其余端点一样须带 API key (免鉴权的监控端点会形同虚设, 与 TTS 门面 /healthz/deep
    同语义)。探测体积极小 (~16KB) 且与真实转写同路径同连接池, 引擎忙到探测超时时
    由探测自身超时快速 503 (degraded), 不排队、不抢占真实流量 (并发取舍见端点注释)。

配置走环境变量 (生产环境经 /etc/chobits-chii-asr.env 注入, 见 docs/deployment.md):
    CHII_ASR_API_KEY          客户端 Bearer 鉴权 key (必填, 未设置拒绝启动)
    CHII_ASR_BACKEND          引擎后端: funasr (默认) | qwen3
    CHII_ASR_ENGINE_HTTP_URL  引擎 HTTP 地址, 默认 http://127.0.0.1:9001
    CHII_ASR_ENGINE_WS_URL    引擎 WS 地址, 默认 ws://127.0.0.1:10095
    CHII_ASR_ENGINE_MODEL     引擎注册的模型名 (funasr --model-path 模式为 custom), 默认 custom
    CHII_ASR_RATE_LIMIT       每 IP 每分钟限流次数 (转写与流式), 默认 60, 设 0 关闭
    CHII_ASR_MAX_UPLOAD_BYTES 批量转写上传上限 (字节), 默认 25MB
    CHII_ASR_WS_MAX_PER_CLIENT  流式每客户端并发连接上限, 默认 4 (按票据身份计数,
                                无身份时回退按 IP; 旧名 CHII_ASR_WS_MAX_PER_IP 兼容读取)
    CHII_ASR_WS_MAX_GLOBAL    流式全局并发连接上限, 默认 32
    CHII_ASR_WS_SESSION_MAX   流式会话最长秒数, 默认 300
    CHII_ASR_WS_IDLE_SECONDS  流式空闲超时秒数 (无帧即断), 默认 60
    CHII_ASR_WS_MAX_AUDIO_BYTES 流式单会话音频总量上限 (字节), 默认 10MB
    CHII_ASR_WS_MAX_FRAME_BYTES 流式客户端单帧音频上限 (字节), 默认 1MB, 超限 error 帧 + close 1009
    CHII_ASR_DEEP_PROBE_TTL    /healthz/deep 探测结果缓存秒数, 默认 30
    CHII_ASR_DEEP_PROBE_TIMEOUT /healthz/deep 单次真实转写探测的超时秒数, 默认 15
    CHII_ASR_SSL_CERTFILE / CHII_ASR_SSL_KEYFILE  同时设置则以 HTTPS/WSS 启动

用法:
    python3 tools/server.py [端口, 默认 9881]   (默认绑 127.0.0.1, 生产由 Caddy 反代;
                                     显式绑非回环地址须同时配 TLS, 否则拒绝启动)

注意: 未启用 TLS 时 API Key 明文传输, 仅适合内网/低风险公网场景;
面向公众分发应用时, 建议由后端服务代为调用, 不要把唯一密钥嵌进客户端。
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import re
import sys
import time
import wave
from collections import defaultdict

from chii_facade_common import (
    ApiKeyAuth,
    EnvConfig,
    SlidingWindowRateLimiter,
    client_ip as _client_ip,
    engine_error_body,
    extract_bearer_token,
    filter_response_headers,
)

# 公共逻辑（env 容错解析 / XFF 真实 IP / key 校验 / 限流桶 / 错误通用化 / 响应头白名单）
# 源自共享库 chii_facade_common（CloudDeploy 仓库 tools/chii-facade-common，兄弟目录 editable 安装）：
# 安全加固只改共享库一处，两门面同步生效，勿在本地重建副本。

_env = EnvConfig("CHII_ASR_")
_getenv = _env.get
_getenv_int = _env.get_int
_getenv_float = _env.get_float


API_KEY = _getenv("API_KEY")
if not API_KEY:
    sys.exit("[错误] 未设置 CHII_ASR_API_KEY 环境变量, 拒绝以无鉴权方式启动")

_auth = ApiKeyAuth(API_KEY)
_key_ok = _auth.key_ok

# 与签发服务 (newapi_provision.py) 共享的 WS 票据签名密钥：
# app 先经垫片 /v1/asr/ticket 换短时票据，再用票据直连 /v1/realtime，
# 门面本地验签即可，无需回调 New API
TICKET_SECRET = _getenv("TICKET_SECRET")
BIND = _getenv("BIND", "127.0.0.1")  # 默认只绑本机（生产由 Caddy 反代）；显式改绑公网见下方 TLS 守卫


def _ticket_verify(ticket: str) -> tuple[bool, str | None, tuple[str, int] | None]:
    """校验短时票据（不核销）：HMAC-SHA256 签名 + 60 秒有效 + jti 查重
    （已核销的 jti 拒收，防 60s 窗口内重放/一票多开）。两种格式均兼容：
      旧版 "<exp>.<jti>.<sig>"（无身份）
      新版 "<exp>.<jti>.<idb64>.<sig>"（idb64 = base64url(调用方身份)，被签名覆盖）
    返回 (是否有效, 身份或 None, 核销凭据 (jti, exp) 或 None)。凭据不在这里写入
    _used_tickets：仅验签与查重前置，真正的核销由 _ticket_mark_used 在
    ws.accept() 之后执行，保证并发超限等拒绝路径不消耗一次性票据。
    身份供按客户端并发计数，无身份时回退按 IP。"""
    if not TICKET_SECRET:
        return False, None, None
    parts = ticket.split(".")
    if len(parts) == 3:
        exp_str, jti, sig = parts
        idb64 = ""
    elif len(parts) == 4:
        exp_str, jti, idb64, sig = parts
    else:
        return False, None, None
    try:
        exp = int(exp_str)
    except ValueError:
        return False, None, None
    now = int(time.time())
    if exp <= now:
        return False, None, None
    if not jti or len(jti) > 64 or len(idb64) > 256:
        return False, None, None
    # 签名形态前置校验：合法 sig 恒为 hexdigest()[:32]（32 位小写 hex）。
    # 非 ASCII 或形态不符直接拒 —— hmac.compare_digest 对含非 ASCII 字符的 str
    # 抛未捕获 TypeError，会把握手打成 500
    if not sig.isascii() or not re.fullmatch(r"[0-9a-f]{32}", sig):
        return False, None, None
    signed = f"asr-realtime.{exp}.{jti}" + (f".{idb64}" if idb64 else "")
    expected = hmac.new(TICKET_SECRET.encode(),
                        signed.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return False, None, None
    identity = None
    if idb64:
        try:
            identity = base64.urlsafe_b64decode(idb64 + "=" * (-len(idb64) % 4)).decode()
        except Exception:
            return False, None, None
    # 查重（不写入）：顺手清掉过期项，再查 jti 是否已核销
    for used_jti in [k for k, used_exp in _used_tickets.items() if used_exp <= now]:
        del _used_tickets[used_jti]
    if jti in _used_tickets:
        return False, None, None
    return True, identity, (jti, exp)


def _ticket_mark_used(jti: str, exp: int) -> None:
    """ws.accept() 之后核销一次性票据（写入 _used_tickets）。
    验签与查重已在 _ticket_verify 前置；验签到核销之间仍有竞态窗口 —— 同一张
    票的两个连接可在窗口内先后通过查重并双双准入。残余窗口可接受：它只在
    客户端主动一票多开时出现，远好于旧语义（先烧后拒，第 5 个并发连接被 4429
    拒绝时票据已作废，换票重连风暴必失败）。"""
    _used_tickets[jti] = exp

RATE_LIMIT = _getenv_int("RATE_LIMIT", 60)
MAX_UPLOAD_BYTES = _getenv_int("MAX_UPLOAD_BYTES", 25 * 1024 * 1024)
# 新名优先；旧名 CHII_ASR_WS_MAX_PER_IP 兼容读取（语义已由每 IP 改为每客户端身份）
WS_MAX_PER_CLIENT = _getenv_int(
    "WS_MAX_PER_CLIENT" if _getenv("WS_MAX_PER_CLIENT") else "WS_MAX_PER_IP", 4)
WS_MAX_GLOBAL = _getenv_int("WS_MAX_GLOBAL", 32)
WS_SESSION_MAX = _getenv_float("WS_SESSION_MAX", 300.0)
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
_used_tickets: dict[str, int] = {}  # jti -> exp，核销集合（过期即清）
_ws_active: dict[str, int] = defaultdict(int)  # 并发桶 ("id:<身份>" 或 "ip:<ip>") -> 在途流式连接数
_ws_active_total = 0

# 滑动窗口限流桶（共享库 SlidingWindowRateLimiter）；RATE_LIMIT 在检查路径上
# 同步进 limiter，保持 monkeypatch 模块常量即时生效的行为不变
_rate_limiter = SlidingWindowRateLimiter(RATE_LIMIT)


def _authorized(request: Request) -> bool:
    # 只认 Authorization 头；不再接受 ?api_key=（query string 会进访问日志）
    return _key_ok(extract_bearer_token(request.headers))


def _rate_ok(ip: str) -> bool:
    """滑动窗口限流: 每 IP 每分钟 RATE_LIMIT 次, 0 关闭。"""
    if RATE_LIMIT <= 0:
        return True
    _rate_limiter.limit = RATE_LIMIT
    return _rate_limiter.allow(ip)


@app.get("/healthz")
async def healthz():
    # 不回 backend 字段（免鉴权端点不暴露引擎指纹）
    return {"status": "ok", "model": ASR_MODEL}


# --- 深度健康检查 -----------------------------------------------------------
# /healthz 是进程级浅探活；/healthz/deep 用一小段内置静音 WAV 走与批量转写相同的
# 引擎 HTTP 路径发一次真实转写探测（不经 WS 票据，直连引擎层），能发现"进程活着但
# 引擎变砖/不可达"的状态。结果缓存 DEEP_PROBE_TTL 秒，单飞锁防并发探测叠加。
# 并发取舍（与 TTS 门面对齐思路但按 ASR 拓扑简化）：门面看不到引擎侧排队深度，
# 不设独立信号量——探测走与真实转写完全相同的 httpx 连接池进引擎（批量路径，
# 生产拓扑里跑 CPU，流式 GPU 会话不受影响），体积 ~16KB，最坏情况只是每 TTL 秒
# 多一条微型请求；引擎忙到探测超时即按 degraded/503 上报由监控重试，不排队、
# 不抢占真实流量。TTL 缓存过期瞬间并发探测：已有缓存吃缓存（过期也回，防监控
# 在探测窗口内抖 503），无缓存（刚启动）快速 503 probe in progress。
DEEP_PROBE_TTL = _getenv_float("DEEP_PROBE_TTL", 30.0)
DEEP_PROBE_TIMEOUT = _getenv_float("DEEP_PROBE_TIMEOUT", 15.0)
_deep_probe: dict = {"at": 0.0, "ok": False, "engine": "not probed yet"}
_deep_probe_lock = asyncio.Lock()


def _make_probe_wav(seconds: float = 0.5) -> bytes:
    """内置深检探测音频：16kHz 16bit 单声道静音 WAV（标准库生成，确定性、无外部资源）。
    静音是对引擎最温和的输入；探测判据是引擎完成转写管线并回良性 JSON（含 text 字段），
    而非识别出具体文字——对静音返回空串 text 属于正常应答，强求出字会让监控必 503。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * int(16000 * seconds))
    return buf.getvalue()


PROBE_WAV = _make_probe_wav()


async def _probe_engine() -> dict:
    """向引擎发一次真实转写探测, 返回 {"ok", "engine"} (engine 为状态简述, 脱敏)。"""
    data = {"model": ENGINE_MODEL}
    files = {"file": ("healthz_probe.wav", PROBE_WAV, "audio/wav")}
    try:
        resp = await _engine.post("/v1/audio/transcriptions", data=data, files=files,
                                  timeout=DEEP_PROBE_TIMEOUT)
    except httpx.HTTPError as exc:
        return {"ok": False, "engine": f"unreachable: {exc.__class__.__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "engine": f"http_{resp.status_code}"}
    try:
        result = json.loads(resp.content)
    except ValueError:
        return {"ok": False, "engine": "bad response"}
    if not isinstance(result, dict) or "text" not in result:
        return {"ok": False, "engine": "bad response"}
    return {"ok": True, "engine": "ok"}


def _deep_probe_response() -> JSONResponse:
    ok = _deep_probe["ok"]
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"ok": ok, "status": "ok" if ok else "degraded",
                 "backend": BACKEND, "engine": _deep_probe["engine"]})


@app.get("/healthz/deep")
async def healthz_deep(request: Request):
    # 鉴权语义与其余端点一致（共享库 ApiKeyAuth，同 /v1/models）：免鉴权的监控端点形同虚设
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if time.time() - _deep_probe["at"] < DEEP_PROBE_TTL:
        return _deep_probe_response()
    # 单飞：上一次探测还在等引擎时不并发重入。检查与获取之间无 await，
    # 在单事件循环内原子完成，不会检后被别人抢先
    if _deep_probe_lock.locked():
        if _deep_probe["at"] > 0:
            return _deep_probe_response()  # 过期缓存也回，防监控在探测窗口内抖 503
        return JSONResponse(status_code=503, content={
            "ok": False, "status": "degraded", "backend": BACKEND,
            "engine": "probe in progress"})
    async with _deep_probe_lock:
        if time.time() - _deep_probe["at"] < DEEP_PROBE_TTL:
            return _deep_probe_response()  # 拿到锁后二次校验：等待期间已被刷新
        result = await _probe_engine()
        _deep_probe.update(result, at=time.time())
        if not result["ok"]:
            sys.stderr.write(f"[healthz] deep probe degraded: {result['engine']}\n")
    return _deep_probe_response()


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
# 引擎响应头白名单（content-type 以外一律不回，防泄露引擎指纹）见共享库
# filter_response_headers 的默认白名单


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
    # 审计日志（journald 自带时间戳）：key 哈希 + 客户端 IP + 请求体长度，
    # 不记明文 key；格式与 TTS 门面中间件审计行一致，同一套 grep/journalctl 可查
    print(f"[audit] /v1/audio/transcriptions"
          f" key={_auth.key_digest(extract_bearer_token(request.headers))}"
          f" ip={_client_ip(request.client.host if request.client else 'unknown', request.headers)}"
          f" len={request.headers.get('content-length', '?')}", file=sys.stderr)
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
    # 引擎错误体不原样回传（含容器内路径/栈细节）：5xx 回写通用错误（对齐 TTS 门面
    # 做法），4xx 保留状态码语义但不带引擎内部细节；原文进门面日志排障
    if resp.status_code >= 400:
        sys.stderr.write(f"[asr] engine {resp.status_code}: {resp.content[:200]!r}\n")
        if resp.status_code >= 500:
            return JSONResponse(engine_error_body("asr"), status_code=502)
        return JSONResponse({"error": "transcription request rejected"},
                            status_code=resp.status_code)
    headers = filter_response_headers(resp.headers)
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    """统一流式识别入口: 鉴权/限流/并发准入后交给当前后端的适配层。

    资源防护（引擎流式会话独占 GPU，不设防时挂死连接即可拖垮全服务）：
    每客户端身份（无身份回退每 IP）/ 全局并发上限、会话最长 WS_SESSION_MAX 秒；
    空闲超时与音频总量上限在适配层逐帧执行（backend_funasr）。"""
    # 限流前置（失败也计桶）；验签前置但此刻不核销 —— 见下方 accept 后的说明
    ip = _client_ip(ws.client.host if ws.client else "unknown", ws.headers)
    if not _rate_ok(ip):
        await ws.close(code=4429)
        return
    # WS 只认 Authorization 头与一次性票据；不再接受 ?api_key=（会进访问日志）
    auth = ws.headers.get("Authorization", "")
    identity = None
    ticket_pending: tuple[str, int] | None = None  # 验签通过，accept 后才核销
    if not (auth.lower().startswith("bearer ") and _key_ok(auth[7:].strip())):
        ok, identity, ticket_pending = _ticket_verify(ws.query_params.get("ticket", ""))
        if not ok:
            await ws.close(code=4401)
            return
    # 并发准入：票据携带身份时按身份计数（同一 NAT/出口 IP 下各客户端独立配额），
    # 无身份（旧票据 / API key 直连）回退按 IP 计数；全局上限不变。
    # 注意：此处拒绝（4429）不得消耗一次性票据，否则客户端并发重连风暴下
    # 被拒连接的票据已被烧掉，换票必然失败
    bucket = f"id:{identity}" if identity else f"ip:{ip}"
    global _ws_active_total
    # .get() 而非 defaultdict[bucket]：直接取值会在拒绝路径上留下 0 值残留项
    if _ws_active_total >= WS_MAX_GLOBAL or _ws_active.get(bucket, 0) >= WS_MAX_PER_CLIENT:
        await ws.close(code=4429)
        return
    # accept 在计数自增之前执行：弱网握手途中断连时 starlette 的 accept 会抛异常
    # ("Cannot call accept once disconnect message has been received")，此刻计数未动，
    # 无泄漏；票据同样不核销（与 mark_used 放在 accept 后同一语义）
    try:
        await ws.accept()
    except Exception:
        return
    # accept 成功后才占并发槽位并核销：任何路径计数配平（finally 必配平一次自增），
    # 并发超限/验签失败/accept 失败的拒绝路径一律不计数不烧票。
    # 剩余竞态窗口（验签→核销之间同 jti 一票多开可双双准入）见 _ticket_mark_used
    _ws_active[bucket] += 1
    _ws_active_total += 1
    if ticket_pending is not None:
        _ticket_mark_used(*ticket_pending)
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
        if _ws_active[bucket] <= 1:
            _ws_active.pop(bucket, None)
        else:
            _ws_active[bucket] -= 1


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else _getenv_int("PORT", 9881)
    scheme = "https" if SSL_CERTFILE else "http"
    print(f"[asr] {scheme}://{BIND}:{port}  backend={BACKEND}"
          f"  /healthz /healthz/deep /v1/models /v1/audio/transcriptions /v1/realtime"
          f" → {ENGINE_HTTP_URL} / {ENGINE_WS_URL}", file=sys.stderr)
    uvicorn.run(app, host=BIND, port=port,
                ssl_certfile=SSL_CERTFILE or None, ssl_keyfile=SSL_KEYFILE or None,
                limit_concurrency=128, timeout_keep_alive=30)


if __name__ == "__main__":
    main()
