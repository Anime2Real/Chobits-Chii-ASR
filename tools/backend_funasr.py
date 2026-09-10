"""Fun-ASR-Nano 后端适配层: 把门面的统一流式协议翻译成 Nano 引擎的 START/STOP 协议.

门面对客户端暴露的统一协议 (WS /v1/realtime):
    客户端 → 服务端:
        文本帧 {"type": "start", "language": "ja"|"zh"|..., "sample_rate": 16000}  (首帧, 必填)
        二进制帧: PCM16 单声道音频块
        文本帧 {"type": "stop"}                                                     (结束)
    服务端 → 客户端:
        {"type": "partial", "text": "..."}   中间结果 (会随更多音频被修正)
        {"type": "final",   "text": "..."}   一句的定稿结果 (按句下推)
        {"type": "error",   "message": "..."} 出错后连接随即关闭

引擎侧 (FunASR serve_realtime_ws.py, 端口默认 10095):
    JSON {"type": "START", ...} → PCM 音频字节流 → JSON {"type": "STOP"};
    引擎返回 {"sentences": [...], "partial": "..."} 序列。

注意: 引擎帧的字段名以构建镜像时锁定的 FunASR commit 为准, 若上游改名只需改本文件。
"""

import asyncio
import json

import websockets

# 客户端发 stop 后等引擎吐完 final 的最长秒数
DRAIN_TIMEOUT = 10.0


def _to_engine_start(start: dict) -> str:
    """统一 start 帧 → Nano START 帧 (语言/采样率透传, 其余字段引擎端自行默认)。"""
    return json.dumps({
        "type": "START",
        "language": start.get("language", "auto"),
        "sample_rate": int(start.get("sample_rate", 16000)),
    }, ensure_ascii=False)


async def _forward_engine_message(client, raw, sentences: list) -> None:
    """Nano 引擎消息 → 统一 partial/final 帧。"""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return
    partial = data.get("partial")
    if partial:
        await client.send_json({"type": "partial", "text": partial})
    for sent in data.get("sentences") or []:
        text = sent.get("text") if isinstance(sent, dict) else str(sent)
        if text and (not sentences or sentences[-1] != text):  # 引擎会重复推全量, 去重
            sentences.append(text)
            await client.send_json({"type": "final", "text": text})


async def handle_realtime(client, engine_url: str) -> None:
    """处理一条 /v1/realtime 连接 (client 为 FastAPI WebSocket, 已 accept)。"""
    try:
        start = await asyncio.wait_for(client.receive_json(), timeout=10.0)
    except Exception:
        await client.close(code=1002)
        return
    if not isinstance(start, dict) or start.get("type") != "start":
        await client.send_json({"type": "error", "message": "首帧必须是 {\"type\": \"start\", ...}"})
        await client.close(code=1002)
        return

    sentences: list = []
    try:
        async with websockets.connect(engine_url, max_size=None) as engine:
            await engine.send(_to_engine_start(start))
            stopped = False

            async def pump_in() -> None:
                """客户端 → 引擎: 二进制帧原样转发, stop 控制帧翻译为 STOP。"""
                nonlocal stopped
                while True:
                    msg = await client.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("bytes") is not None:
                        await engine.send(msg["bytes"])
                    elif msg.get("text"):
                        try:
                            ctrl = json.loads(msg["text"])
                        except json.JSONDecodeError:
                            continue
                        if ctrl.get("type") == "stop":
                            stopped = True
                            await engine.send(json.dumps({"type": "STOP"}))
                            return

            async def pump_out() -> None:
                """引擎 → 客户端, 引擎关闭连接时结束。"""
                async for raw in engine:
                    await _forward_engine_message(client, raw, sentences)

            t_in = asyncio.create_task(pump_in())
            t_out = asyncio.create_task(pump_out())
            done, pending = await asyncio.wait({t_in, t_out},
                                               return_when=asyncio.FIRST_COMPLETED)
            # 客户端侧先结束: 补发 STOP (客户端断开时), 并等引擎把 final 吐完
            if t_in in done and not t_out.done():
                if not stopped:
                    try:
                        await engine.send(json.dumps({"type": "STOP"}))
                    except websockets.ConnectionClosed:
                        pass
                try:
                    await asyncio.wait_for(asyncio.shield(t_out), timeout=DRAIN_TIMEOUT)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    pass
            for t in pending:
                t.cancel()
    except (OSError, websockets.WebSocketException) as e:
        try:
            await client.send_json({"type": "error",
                                    "message": f"引擎连接失败: {type(e).__name__}"})
            await client.close(code=1011)
        except Exception:
            pass
