"""Qwen3-ASR 后端适配层 (预留骨架).

HTTP 批量转写: Qwen3-ASR 的 qwen-asr-serve / vllm serve 原生暴露
POST /v1/audio/transcriptions, 与 Fun-ASR-Nano 完全一致, 门面直接透传引擎地址即可,
无需任何适配——切换后端只需改 CHII_ASR_BACKEND 与两个引擎地址环境变量。

WS 流式: 尚未实现。Qwen3-ASR 的流式目前只有 Python SDK 形态
(qwen-asr[vllm] 的 init_streaming_state / streaming_generate), 引擎端没有
OpenAI Realtime 兼容端点。启用步骤 (Roadmap):
    1. 在 Qwen3-ASR 引擎容器内加一个基于其 streaming SDK 的 WS shim,
       对本适配层暴露与 Fun-ASR-Nano 相同的 START/音频帧/STOP 协议;
    2. 本文件的 handle_realtime 直接复用 backend_funasr 的协议翻译
       (from backend_funasr import handle_realtime 即可)。
"""


async def handle_realtime(client, engine_url: str) -> None:
    """流式接口暂未就绪: 明确告知客户端而不是静默失败。"""
    await client.send_json({
        "type": "error",
        # 枚举见 backend_funasr.ERROR_CODES；未就绪属其他未归类
        "code": "internal_error",
        "message": "Qwen3-ASR 后端暂不支持流式识别 (WS shim 未实现), "
                   "请使用 POST /v1/audio/transcriptions 批量转写, 详见 README Roadmap",
    })
    await client.close(code=1008)
