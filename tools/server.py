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
    CHII_ASR_SSL_CERTFILE / CHII_ASR_SSL_KEYFILE  同时设置则以 HTTPS/WSS 启动

用法:
    python3 tools/server.py [端口, 默认 9881]

注意: 未启用 TLS 时 API Key 明文传输, 仅适合内网/低风险公网场景;
面向公众分发应用时, 建议由后端服务代为调用, 不要把唯一密钥嵌进客户端。
"""

import os
import sys
import time
from collections import defaultdict, deque


def _getenv(suffix: str, default: str = "") -> str:
    """读取 CHII_ASR_<suffix> 环境变量。"""
    return os.environ.get(f"CHII_ASR_{suffix}", default)


API_KEY = _getenv("API_KEY")
if not API_KEY:
    sys.exit("[错误] 未设置 CHII_ASR_API_KEY 环境变量, 拒绝以无鉴权方式启动")

RATE_LIMIT = int(_getenv("RATE_LIMIT", "60"))
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
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "content-length",
              "content-encoding", "te", "trailers", "upgrade"}

app = FastAPI(title="chobits-chii-asr", docs_url=None, redoc_url=None)
_engine = httpx.AsyncClient(base_url=ENGINE_HTTP_URL, timeout=httpx.Timeout(300.0))
_hits: dict[str, deque] = defaultdict(deque)


def _authorized(request: Request) -> bool:
    if request.headers.get("Authorization", "") == f"Bearer {API_KEY}":
        return True
    return request.query_params.get("api_key") == API_KEY


def _rate_ok(ip: str) -> bool:
    """滑动窗口限流: 每 IP 每分钟 RATE_LIMIT 次, 0 关闭。"""
    if RATE_LIMIT <= 0:
        return True
    now = time.monotonic()
    q = _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
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


@app.post("/v1/audio/transcriptions")
async def transcriptions(request: Request):
    """OpenAI 批量转写垫片: 透传 multipart 表单, 仅把 model 字段改写为引擎注册名。

    引擎只认自己注册的模型名 (见 ENGINE_MODEL), 对外统一暴露 chii-asr;
    文件与其余表单字段 (language/prompt 等) 原样转发, 响应原样回传。"""
    if not _authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _rate_ok(request.client.host if request.client else "unknown"):
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
    data: dict = {}
    files: dict = {}
    for key, value in (await request.form()).multi_items():
        if key == "model":
            data[key] = ENGINE_MODEL
        elif isinstance(value, UploadFile):
            files[key] = (value.filename, await value.read(), value.content_type)
        else:
            data[key] = value
    if "model" not in data:
        data["model"] = ENGINE_MODEL
    try:
        resp = await _engine.post("/v1/audio/transcriptions", data=data, files=files)
    except httpx.HTTPError as e:
        return JSONResponse({"error": f"upstream error: {type(e).__name__}"},
                            status_code=502)
    headers = {k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP}
    return Response(content=resp.content, status_code=resp.status_code, headers=headers)


@app.websocket("/v1/realtime")
async def realtime(ws: WebSocket):
    """统一流式识别入口: 鉴权/限流后交给当前后端的适配层。"""
    if ws.query_params.get("api_key") != API_KEY and \
            ws.headers.get("Authorization", "") != f"Bearer {API_KEY}":
        await ws.close(code=4401)
        return
    if not _rate_ok(ws.client.host if ws.client else "unknown"):
        await ws.close(code=4429)
        return
    await ws.accept()
    try:
        await backend.handle_realtime(ws, ENGINE_WS_URL)
    except Exception as e:  # 适配层异常不暴露内部细节
        sys.stderr.write(f"[asr] realtime aborted: {type(e).__name__}: {e}\n")
        try:
            await ws.close(code=1011)
        except Exception:
            pass


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(_getenv("PORT", "9881"))
    scheme = "https" if SSL_CERTFILE else "http"
    print(f"[asr] {scheme}://0.0.0.0:{port}  backend={BACKEND}"
          f"  /v1/models /v1/audio/transcriptions /v1/realtime"
          f" → {ENGINE_HTTP_URL} / {ENGINE_WS_URL}", file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=port,
                ssl_certfile=SSL_CERTFILE or None, ssl_keyfile=SSL_KEYFILE or None)


if __name__ == "__main__":
    main()
