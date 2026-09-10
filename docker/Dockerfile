# Fun-ASR-Nano-2512 推理引擎镜像
# 单容器双进程 (均为 funasr pip 包自带 console script):
#   funasr-server           OpenAI 兼容 HTTP 批量转写 (POST /v1/audio/transcriptions, :9001)
#   funasr-realtime-server  流式 WebSocket 服务 (START/音频帧/STOP 协议, :10095)
#
# 构建:  docker build -t chobits-chii-asr-engine docker/
# 运行见 docs/deployment.md; 模型权重首次启动经 ModelScope 下载, 挂载缓存卷持久化
FROM pytorch/pytorch:2.9.0-cuda12.8-cudnn9-runtime

# 国内网络构建超时时可注入 PyPI 镜像 (不写默认值, 不影响海外构建), 如:
#   --build-arg PIP_INDEX_URL=http://mirrors.tencentyun.com/pypi/simple \
#   --build-arg PIP_TRUSTED_HOST=mirrors.tencentyun.com
ARG PIP_INDEX_URL=""
ARG PIP_TRUSTED_HOST=""

# triton (vLLM 依赖) 在运行时要 JIT 编译 kernel, runtime 镜像无 C 编译器 → 装 gcc;
# 国内 apt 慢可 --build-arg APT_MIRROR=mirrors.tencentyun.com 换内网源
ARG APT_MIRROR=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|//[^ ]*archive.ubuntu.com|//$APT_MIRROR|g; s|//security.ubuntu.com|//$APT_MIRROR|g" /etc/apt/sources.list; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# funasr 1.4.x 服务栈: 批量/流式引擎都基于 vLLM (批量在 vLLM 不可用时回退 AutoModel);
# fastapi/uvicorn/python-multipart 是 funasr-server 的运行时依赖 (funasr 未声明)
RUN if [ -n "$PIP_INDEX_URL" ]; then pip config set global.index-url "$PIP_INDEX_URL"; fi \
    && if [ -n "$PIP_TRUSTED_HOST" ]; then pip config set global.trusted-host "$PIP_TRUSTED_HOST"; fi \
    && pip install --no-cache-dir \
        "funasr>=1.3.26" \
        "torchaudio>=2.9.0" \
        "transformers>=4.51.3" \
        vllm fastapi uvicorn python-multipart \
        modelscope soundfile librosa websockets

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# funasr _server_app 的批量 vLLM 尝试硬编码 bf16+0.5 显存, 在 T4 上注定失败且每次残留数 GB 孤儿显存。
# 打补丁加 FUNASR_SERVER_NO_VLLM 开关: 置 1 时直接走 AutoModel 回退 (bf16 卡可 -e FUNASR_SERVER_NO_VLLM= 恢复)
RUN python -c "import pathlib; p = pathlib.Path('/opt/conda/lib/python3.11/site-packages/funasr/bin/_server_app.py'); src = p.read_text(); old = '        try:\n            from funasr.models.fun_asr_nano.inference_vllm import FunASRNanoVLLM'; new = '        try:\n            if os.environ.get(\"FUNASR_SERVER_NO_VLLM\"):\n                raise RuntimeError(\"vLLM disabled by FUNASR_SERVER_NO_VLLM\")\n            from funasr.models.fun_asr_nano.inference_vllm import FunASRNanoVLLM'; assert src.count(old) == 1, 'patch anchor not found'; p.write_text(src.replace(old, new)); print('patched _server_app.py: FUNASR_SERVER_NO_VLLM guard added')"

# 模型与运行参数 (均可 docker run -e 覆盖)
# DTYPE=fp32: T4 (sm75) 无 bf16 硬件单元; 引擎把 fp16 静默映射成 bf16 (会崩), fp32 是官方给非 bf16 卡的路径
# WS_GPU_MEM_UTIL: WS 流式 vLLM 显存配比 (预算含其他进程占用; 批量侧 AutoModel 约 2.4GB)
ENV MODEL_ID=FunAudioLLM/Fun-ASR-Nano-2512 \
    DEVICE=cuda:0 \
    HUB=ms \
    LANGUAGE=日本語 \
    DTYPE=fp32 \
    WS_GPU_MEM_UTIL=0.55 \
    HTTP_PORT=9001 \
    WS_PORT=10095

EXPOSE 9001 10095
WORKDIR /app
ENTRYPOINT ["/app/entrypoint.sh"]
