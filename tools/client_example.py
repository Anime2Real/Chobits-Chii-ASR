#!/usr/bin/env python3
"""小叽 ASR 客户端调用示例: 批量转写 + 流式识别。

环境变量:
    CHII_ASR_API_KEY   门面鉴权 key (必填)
    CHII_ASR_BASE_URL  门面地址, 默认 http://127.0.0.1:9881

用法:
    # 批量转写 (OpenAI 兼容协议)
    python3 tools/client_example.py batch audio.wav

    # 流式识别 (把 wav 按 100ms 一块实时推流, 模拟麦克风; 输出 partial/final)
    python3 tools/client_example.py stream audio.wav [语言, 默认 ja]

依赖: httpx, websockets (即门面 requirements.txt 的子集)。
"""

import asyncio
import json
import os
import sys
import wave

import httpx
import websockets

BASE_URL = os.environ.get("CHII_ASR_BASE_URL", "http://127.0.0.1:9881")
API_KEY = os.environ.get("CHII_ASR_API_KEY", "")
if not API_KEY:
    sys.exit("[错误] 请设置 CHII_ASR_API_KEY")


def batch(path: str) -> None:
    with open(path, "rb") as f:
        resp = httpx.post(
            f"{BASE_URL}/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            files={"file": (os.path.basename(path), f)},
            data={"model": "chii-asr"},
            timeout=300.0)
    resp.raise_for_status()
    print(resp.json()["text"])


async def stream(path: str, language: str) -> None:
    # 统一 WS 协议: 首帧 start → PCM16 二进制帧 → stop; 服务端回 partial/final
    with wave.open(path, "rb") as wf:
        if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            sys.exit("[错误] 流式示例要求 16-bit 单声道 wav (可用 ffmpeg 转换: "
                     "ffmpeg -i in.mp3 -ar 16000 -ac 1 -f wav out.wav)")
        sample_rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())

    ws_url = BASE_URL.replace("http", "ws", 1) + "/v1/realtime"
    chunk = sample_rate * 2 // 10  # 100ms 一块 (PCM16 = 2 字节/采样)
    async with websockets.connect(f"{ws_url}?api_key={API_KEY}", max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "language": language,
                                  "sample_rate": sample_rate}, ensure_ascii=False))

        async def send_audio() -> None:
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i + chunk])
                await asyncio.sleep(0.1)  # 模拟实时采集节奏
            await ws.send(json.dumps({"type": "stop"}))

        async def recv_text() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                if msg["type"] == "partial":
                    print(f"\r[partial] {msg['text']}", end="", flush=True)
                elif msg["type"] == "final":
                    print(f"\n[final] {msg['text']}")
                elif msg["type"] == "error":
                    print(f"\n[error] {msg['message']}")
                    return

        await asyncio.gather(send_audio(), recv_text())


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("batch", "stream"):
        sys.exit(__doc__)
    if sys.argv[1] == "batch":
        batch(sys.argv[2])
    else:
        asyncio.run(stream(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "ja"))
