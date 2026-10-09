# Qwen3-ASR-1.7B 推理引擎镜像 (分支: qwen3-asr-1.7b)
# 基于官方镜像, 单进程:
#   qwen-asr-serve  OpenAI 兼容 HTTP 批量转写 (POST /v1/audio/transcriptions, :8000, vLLM)
#
# 流式: Qwen3-ASR 官方仅有 SDK 级流式 (init_streaming_state/streaming_generate),
#       无现成 WS 服务端点; 门面 /v1/realtime 由 backend_qwen3 明确报错 (见 README Roadmap)。
#
# 构建:  docker build -t chobits-chii-asr-engine-qwen3 docker/
# 运行 (Tesla T4 实测参数, 2026-10-09):
#   docker run -d --name chobits-chii-asr-engine --gpus all --restart unless-stopped \
#     -p 127.0.0.1:9001:8000 \
#     -e NVIDIA_DISABLE_REQUIRE=1 \                  # 驱动 <565 (CUDA<12.8) 需绕过镜像的版本检查
#     -e VLLM_ATTENTION_BACKEND=TRITON_ATTN \        # sm75 (T4) 上 flashinfer 后端启动卡死
#     -e HF_ENDPOINT=https://hf-mirror.com \         # 国内直连 huggingface.co 不可达时
#     -v $HOME/.cache/huggingface:/root/.cache/huggingface \
#     chobits-chii-asr-engine-qwen3
#   (宿主 9001 与门面默认 CHII_ASR_ENGINE_HTTP_URL 对齐; 权重首启自动下载)
FROM qwenllm/qwen3-asr:latest

# 模型与运行参数 (均可 docker run -e 覆盖; T4 实测见 entrypoint.sh 头注释)
# GPU_MEM_UTIL: vLLM 显存配比。T4 16GB 与 TTS 共存时 0.6 实测可用 (0.8 CUDA graph 捕获期 OOM)
ENV MODEL_ID=Qwen/Qwen3-ASR-1.7B \
    PORT=8000 \
    GPU_MEM_UTIL=0.6 \
    MAX_MODEL_LEN=32768 \
    ENFORCE_EAGER=1

EXPOSE 8000
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
