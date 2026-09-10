"""Fun-ASR-Nano 后端适配层: 把门面的统一流式协议翻译成 Nano 引擎的实时 WS 协议.

门面对客户端暴露的统一协议 (WS /v1/realtime):
    客户端 → 服务端:
        文本帧 {"type": "start", "language": "ja"|"zh"|..., "sample_rate": 16000}  (首帧, 必填)
        二进制帧: PCM16 单声道音频块 (必须 16kHz, 引擎端固定 16k 不重采样)
        文本帧 {"type": "stop"}                                                     (结束)
    服务端 → 客户端:
        {"type": "partial", "text": "..."}   中间结果 (会随更多音频被修正)
        {"type": "final",   "text": "..."}   一句的定稿结果 (按句下推)
        {"type": "error",   "message": "..."} 出错后连接随即关闭

引擎侧 (funasr >=1.4 的 funasr.bin.realtime_ws, 端口默认 10095):
    控制帧是纯文本命令而非 JSON: "START" / "STOP" / "LANGUAGE:<提示语>" / "HOTWORDS:a,b";
    音频帧为 16kHz PCM16 字节流。引擎回包:
        {"event": "started"|"stopped"|"language_set"|"error", ...}   事件帧
        {"sentences": [{"text","start","end"}], "partial": "...",
         "is_final": bool, ...}                                       识别结果帧
    注意 STOP 之后引擎不关闭连接 (可再次 START), 由本适配层在收到 stopped/is_final 后主动断开。
"""

import asyncio
import json

import websockets

# 客户端发 stop 后等引擎吐完 final 的最长秒数
DRAIN_TIMEOUT = 10.0

# 统一协议的 ISO 语言码 → 引擎语言提示语 (引擎 --language 示例: 中文, English, 日本語)
LANGUAGE_MAP = {"ja": "日本語", "zh": "中文", "en": "English"}


def _to_engine_commands(start: dict) -> list:
    """统一 start 帧 → 引擎命令序列 (START 后按需要追加 LANGUAGE 等)。"""
    cmds = ["START"]
    lang = str(start.get("language") or "")
    if lang and lang != "auto":
        cmds.append(f"LANGUAGE:{LANGUAGE_MAP.get(lang, lang)}")
    return cmds


async def _forward_engine_message(client, raw, sentences: list) -> bool:
    """Nano 引擎消息 → 统一 partial/final 帧。返回 True 表示本次会话应结束。"""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    event = data.get("event")
    if event == "error":
        await client.send_json({"type": "error",
                                "message": str(data.get("error", "engine error"))})
        return True
    if event == "stopped":
        return True
    partial = data.get("partial")
    if partial:
        await client.send_json({"type": "partial", "text": partial})
    for sent in data.get("sentences") or []:
        text = sent.get("text") if isinstance(sent, dict) else str(sent)
        if text and (not sentences or sentences[-1] != text):  # 引擎会重复推全量, 去重
            sentences.append(text)
            await client.send_json({"type": "final", "text": text})
    return bool(data.get("is_final"))


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
    if int(start.get("sample_rate", 16000)) != 16000:
        await client.send_json({"type": "error",
                                "message": "Fun-ASR-Nano 引擎只接受 16kHz PCM16, 请客户端先重采样"})
        await client.close(code=1002)
        return

    sentences: list = []
    try:
        async with websockets.connect(engine_url, max_size=None) as engine:
            for cmd in _to_engine_commands(start):
                await engine.send(cmd)
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
                            await engine.send("STOP")
                            return

            async def pump_out() -> None:
                """引擎 → 客户端, 收到 stopped/is_final/error 即结束 (引擎不关连接, 主动断)。"""
                async for raw in engine:
                    if await _forward_engine_message(client, raw, sentences):
                        return

            t_in = asyncio.create_task(pump_in())
            t_out = asyncio.create_task(pump_out())
            done, pending = await asyncio.wait({t_in, t_out},
                                               return_when=asyncio.FIRST_COMPLETED)
            # 客户端侧先结束: 补发 STOP (客户端断开时), 并等引擎把 final 吐完
            if t_in in done and not t_out.done():
                if not stopped:
                    try:
                        await engine.send("STOP")
                    except websockets.ConnectionClosed:
                        pass
                try:
                    await asyncio.wait_for(asyncio.shield(t_out), timeout=DRAIN_TIMEOUT)
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    pass
            for t in pending:
                t.cancel()
            # 结果已吐完, 礼貌关闭客户端连接 (直接 return 会没有 close 帧)
            try:
                await client.close()
            except Exception:
                pass
    except (OSError, websockets.WebSocketException) as e:
        try:
            await client.send_json({"type": "error",
                                    "message": f"引擎连接失败: {type(e).__name__}"})
            await client.close(code=1011)
        except Exception:
            pass
