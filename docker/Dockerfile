# Qwen3-ASR-0.6B 推理引擎镜像 (分支: qwen3-asr-0.6b)
# 基于官方镜像, 单进程:
#   qwen-asr-serve  OpenAI 兼容 HTTP 批量转写 (POST /v1/audio/transcriptions, :8000, vLLM)
#
# 流式: Qwen3-ASR 官方仅有 SDK 级流式 (init_streaming_state/streaming_generate),
#       无现成 WS 服务端点; 门面 /v1/realtime 由 backend_qwen3 明确报错 (见 README Roadmap)。
#
# 构建:  docker build -t chobits-chii-asr-engine-qwen3 docker/
# 运行:  docker run -d --name chobits-chii-asr-engine --gpus all --restart unless-stopped \
#          -p 127.0.0.1:9001:8000 \
#          -v $HOME/.cache/huggingface:/root/.cache/huggingface \
#          chobits-chii-asr-engine-qwen3
#        (宿主 9001 与门面默认 CHII_ASR_ENGINE_HTTP_URL 对齐; 权重首启自动下载)
FROM qwenllm/qwen3-asr:latest

# 模型与运行参数 (均可 docker run -e 覆盖)
# GPU_MEM_UTIL: vLLM 显存配比, 单进程独占单卡时给足即可 (T4 16GB 实测 0.8 充裕)
ENV MODEL_ID=Qwen/Qwen3-ASR-0.6B \
    PORT=8000 \
    GPU_MEM_UTIL=0.8

EXPOSE 8000
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh
ENTRYPOINT ["/app/entrypoint.sh"]
