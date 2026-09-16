"""Fun-ASR-Nano 后端适配层: 把门面的统一流式协议翻译成 Nano 引擎的实时 WS 协议.

门面对客户端暴露的统一协议 (WS /v1/realtime):
    客户端 → 服务端:
        文本帧 {"type": "start", "language": "ja"|"zh"|..., "sample_rate": 16000}  (首帧, 必填)
        二进制帧: PCM16 单声道音频块 (必须 16kHz, 引擎端固定 16k 不重采样; 单帧 ≤1MB,
                  超限直接 error 帧 + close 1009，不进引擎)
        文本帧 {"type": "stop"}                                                     (结束)
    服务端 → 客户端:
        {"type": "partial", "text": "..."}   中间结果 (会随更多音频被修正)
        {"type": "final",   "text": "..."}   一句的定稿结果 (按句下推)
        {"type": "error",   "code": "...", "message": "..."} 出错后连接随即关闭

    error 帧的 code 为稳定枚举 (X-6 协议, 与 LLM/Mascot 门面同一集合, 勿自行增删):
        engine_error           引擎识别/处理错误 (含引擎侧 PayloadTooBig 等异常 surfaced)
        engine_conn_failed     连不上引擎 (accept 超时/拒绝)
        idle_timeout           空闲超时关闭
        protocol_error         协议错误 (首帧不是 start、start 参数非法等)
        frame_too_large        单帧超 CHII_ASR_WS_MAX_FRAME_BYTES
        audio_limit_exceeded   会话音频总量上限触顶
        internal_error         其他未归类
    message 为中文兜底文案, 客户端应按 code 本地化, 勿反解 message 文本。

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
import os
import sys

import websockets

# 客户端发 stop 后等引擎吐完 final 的最长秒数
DRAIN_TIMEOUT = 10.0


def _env_float(name: str, default: float) -> float:
    """数值配置容错：非法值回退默认并告警（裸 float()/int() 的 traceback 会被
    Restart=always 放大成崩溃循环；与 tools/server.py 门面侧 helper 同款）。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        sys.stderr.write(f"[asr] [警告] {name}={raw!r} 不是合法数值，回退默认值 {default}\n")
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        sys.stderr.write(f"[asr] [警告] {name}={raw!r} 不是合法整数，回退默认值 {default}\n")
        return default


# 资源防护（环境变量可调）：空闲超时（无帧即断）与单会话音频总量上限。
# 不设防时客户端发完 start 后挂起不动即可永久占用一条引擎流式会话（独占 GPU）
IDLE_TIMEOUT = _env_float("CHII_ASR_WS_IDLE_SECONDS", 60.0)
MAX_AUDIO_BYTES = _env_int("CHII_ASR_WS_MAX_AUDIO_BYTES", 10 * 1024 * 1024)

# 客户端单帧音频上限（默认 1MB）：16kHz PCM16 实时码率仅 32KB/s，单帧超 1MB 必是
# 异常/恶意客户端；超限直接 error 帧 + close 1009 (message too big)，帧不进引擎
MAX_FRAME_BYTES = _env_int("CHII_ASR_WS_MAX_FRAME_BYTES", 1024 * 1024)

# WS error 帧的 code 枚举（X-6 协议契约，与 LLM/Mascot 门面一致，勿自行增删枚举值；
# message 为中文兜底文案，客户端按 code 本地化）。会话时长上限（WS_SESSION_MAX，
# server.py）实现为直接 close 1011 而非 error 帧，故不占用本枚举。
ERROR_CODES = frozenset({
    "engine_error",
    "engine_conn_failed",
    "idle_timeout",
    "protocol_error",
    "frame_too_large",
    "audio_limit_exceeded",
    "internal_error",
})

# 引擎入站消息上限（4MB）：引擎每条消息都带全量 sentences 历史，长会话单消息持续增长，
# max_size=None 等于放任单条消息撑爆内存。超限时 websockets 抛 PayloadTooBig
# (WebSocketException 子类)，经 handle_realtime 异常路径兜住后客户端收到 error 帧
ENGINE_MAX_MSG_BYTES = 4 * 1024 * 1024

# 统一协议的 ISO 语言码 → 引擎语言提示语 (引擎 --language 示例: 中文, English, 日本語)
LANGUAGE_MAP = {"ja": "日本語", "zh": "中文", "en": "English"}


def _to_engine_commands(start: dict) -> list:
    """统一 start 帧 → 引擎命令序列 (START 后按需要追加 LANGUAGE 等)。

    language 只接受 LANGUAGE_MAP 的键：引擎控制帧是纯文本命令 (START/LANGUAGE:/
    HOTWORDS: 等), 白名单外的值原样透传等于让客户端直接写引擎命令行。"""
    cmds = ["START"]
    lang = str(start.get("language") or "")
    if lang and lang != "auto":
        if lang not in LANGUAGE_MAP:
            raise ValueError(f"unsupported language: {lang!r}")
        cmds.append(f"LANGUAGE:{LANGUAGE_MAP[lang]}")
    return cmds


async def _forward_engine_message(client, raw, sentences: list) -> bool:
    """Nano 引擎消息 → 统一 partial/final 帧。返回 True 表示本次会话应结束。

    引擎每条消息都带全量 sentences 历史, sentences 参数按下标记录已下推进度
    (与引擎数组严格对齐, 含空文本占位), 只发新增的尾巴——按文本去重会同时造成
    "不同句级联重复"和"连续相同句被吞"两类错误。引擎偶尔会原地扩写最后一句
    (boundary retry), 已下推的下标不重发, 该扩写会被跳过 (可接受的极端边角)。"""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    event = data.get("event")
    if event == "error":
        # 引擎错误原文不外发（含内部细节），固定文案；原文进门面日志排障
        sys.stderr.write(f"[asr] engine error event: {data.get('error')!r}\n")
        await client.send_json({"type": "error", "code": "engine_error",
                                "message": "引擎识别错误，请重试"})
        return True
    if event == "stopped":
        return True
    partial = data.get("partial")
    if partial:
        await client.send_json({"type": "partial", "text": partial})
    for i, sent in enumerate(data.get("sentences") or []):
        if i < len(sentences):
            continue
        text = sent.get("text") if isinstance(sent, dict) else str(sent)
        sentences.append(text or "")
        if text:
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
        await client.send_json({"type": "error", "code": "protocol_error",
                                "message": "首帧必须是 {\"type\": \"start\", ...}"})
        await client.close(code=1002)
        return
    raw_rate = start.get("sample_rate")
    try:
        sample_rate = int(16000 if raw_rate is None else raw_rate)
    except (TypeError, ValueError):
        await client.send_json({"type": "error", "code": "protocol_error",
                                "message": "sample_rate 必须是整数（目前只支持 16000）"})
        await client.close(code=1002)
        return
    if sample_rate != 16000:
        await client.send_json({"type": "error", "code": "protocol_error",
                                "message": "Fun-ASR-Nano 引擎只接受 16kHz PCM16, 请客户端先重采样"})
        await client.close(code=1002)
        return

    sentences: list = []
    try:
        commands = _to_engine_commands(start)
    except ValueError as e:
        await client.send_json({"type": "error", "code": "protocol_error", "message": str(e)})
        await client.close(code=1002)
        return
    try:
        async with websockets.connect(engine_url, max_size=ENGINE_MAX_MSG_BYTES) as engine:
            for cmd in commands:
                await engine.send(cmd)
            stopped = False
            client_gone = False

            async def pump_in() -> None:
                """客户端 → 引擎: 二进制帧原样转发, stop 控制帧翻译为 STOP。
                逐帧执行空闲超时与音频总量上限，超限主动结束会话。"""
                nonlocal stopped, client_gone
                audio_bytes = 0
                while True:
                    try:
                        msg = await asyncio.wait_for(client.receive(), timeout=IDLE_TIMEOUT)
                    except asyncio.TimeoutError:
                        try:
                            await client.send_json({"type": "error", "code": "idle_timeout",
                                                    "message": "空闲超时，连接关闭"})
                        except Exception:
                            pass
                        return
                    if msg["type"] == "websocket.disconnect":
                        client_gone = True
                        return
                    if msg.get("bytes") is not None:
                        frame = msg["bytes"]
                        if len(frame) > MAX_FRAME_BYTES:
                            try:
                                await client.send_json({"type": "error", "code": "frame_too_large",
                                                        "message": "单帧音频超限（1MB），连接关闭"})
                                await client.close(code=1009)
                            except Exception:
                                pass
                            return
                        audio_bytes += len(frame)
                        if audio_bytes > MAX_AUDIO_BYTES:
                            try:
                                await client.send_json({"type": "error", "code": "audio_limit_exceeded",
                                                        "message": "音频总量超限，连接关闭"})
                            except Exception:
                                pass
                            return
                        await engine.send(frame)
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
            # 已完成任务如带异常必须 retrieve，否则 GC 时抛 "exception was never retrieved"
            pump_failed = False
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    sys.stderr.write(f"[asr] realtime pump failed: {t.exception()!r}\n")
                    pump_failed = True
            # 客户端侧先结束: 补发 STOP (客户端断开时), 并等引擎把 final 吐完。
            # 客户端已断开时跳过——补发/drain 只对还连着、主动 stop 的客户端有意义
            if t_in in done and not t_out.done() and not client_gone:
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
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            # 泵带异常结束（如引擎消息超 ENGINE_MAX_MSG_BYTES 触发 PayloadTooBig）：
            # 细节已落门面日志，客户端只给通用 error 帧再关（正常 is_final/stop 结束无此帧）
            if pump_failed and not client_gone:
                try:
                    await client.send_json({"type": "error", "code": "engine_error",
                                            "message": "引擎连接异常，识别中断"})
                except Exception:
                    pass
            # 结果已吐完, 礼貌关闭客户端连接 (直接 return 会没有 close 帧)
            try:
                await client.close()
            except Exception:
                pass
    except (OSError, websockets.WebSocketException) as e:
        sys.stderr.write(f"[asr] engine connect failed: {type(e).__name__}: {e}\n")
        try:
            await client.send_json({"type": "error", "code": "engine_conn_failed",
                                    "message": "引擎连接失败"})
            await client.close(code=1011)
        except Exception:
            pass
