# Fun-ASR-Nano-2512 推理引擎镜像
# 单容器双进程:
#   funasr-server         OpenAI 兼容 HTTP 批量转写 (POST /v1/audio/transcriptions, :9001)
#   serve_realtime_ws.py  官方流式 WebSocket 服务 (START/音频帧/STOP 协议, :10095)
#
# 构建:  docker build -t chobits-chii-asr-engine docker/
# 运行见 docs/deployment.md; 模型权重首次启动经 ModelScope 下载, 挂载缓存卷持久化
FROM pytorch/pytorch:2.9.0-cuda12.8-cudnn9-runtime

# 官方流式脚本不在 pip 包内, 需从 FunASR 仓库拉取;
# 生产环境请把该 ref 锁到验证过的 commit/tag, 避免 main 分支漂移
ARG FUNASR_REPO_REF=main

RUN pip install --no-cache-dir \
        "funasr>=1.3.26" \
        "torchaudio>=2.9.0" \
        "transformers>=4.51.3" \
        modelscope soundfile librosa websockets \
    && git clone --depth 1 --branch "$FUNASR_REPO_REF" \
        https://github.com/modelscope/FunASR.git /opt/funasr-repo \
    && mkdir -p /app \
    && cp /opt/funasr-repo/examples/industrial_data_pretraining/fun_asr_nano/serve_realtime_ws.py /app/

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# 模型与运行参数 (均可 docker run -e 覆盖)
ENV MODEL_ID=FunAudioLLM/Fun-ASR-Nano-2512 \
    DEVICE=cuda:0 \
    HUB=ms \
    LANGUAGE=中文 \
    HTTP_PORT=9001 \
    WS_PORT=10095

EXPOSE 9001 10095
WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
