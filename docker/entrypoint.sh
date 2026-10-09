#!/bin/bash
# Qwen3-ASR 引擎容器入口: 单进程 HTTP 批量转写 (qwen-asr-serve, OpenAI 兼容, vLLM)
# 无 WS 流式服务 (官方 SDK 级流式未包装为服务端点, 见 README Roadmap)
#
# Tesla T4 (sm75, 驱动 525/CUDA 12.0) 实测适配 (2026-10-09 部署验证):
#   - 镜像声明 cuda>=12.8, 驱动不足时 docker run 加 -e NVIDIA_DISABLE_REQUIRE=1
#   - sm75 上 flashinfer attention 后端启动卡死, 须 -e VLLM_ATTENTION_BACKEND=TRITON_ATTN
#   - 默认 max-model-len 65536 的 KV 预算 ~7GB, T4 装不下 → 默认限 32768
#   - 国内直连 huggingface.co 不可达时 -e HF_ENDPOINT=https://hf-mirror.com
set -euo pipefail

MODEL_ID="${MODEL_ID:-Qwen/Qwen3-ASR-1.7B}"
PORT="${PORT:-8000}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.6}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
# T4 实测 1.7B CUDA graph 捕获期 OOM, 默认 eager (不捕获 graph); 显存充裕的新卡可 -e ENFORCE_EAGER= 置空
ENFORCE_EAGER="${ENFORCE_EAGER:-1}"

EXTRA_ARGS=()
if [ -n "$ENFORCE_EAGER" ]; then
    EXTRA_ARGS+=(--enforce-eager)
fi

echo "[engine] HTTP 批量转写: qwen-asr-serve model=$MODEL_ID port=$PORT" \
    "gpu-mem-util=$GPU_MEM_UTIL max-model-len=$MAX_MODEL_LEN eager=$ENFORCE_EAGER"
# exec 为 PID 1: docker stop 的 TERM 直达 qwen-asr-serve, 由 vLLM 完成优雅卸载。
# --host/--port/--gpu-memory-utilization/--max-model-len 均透传给底层 vllm serve
exec qwen-asr-serve "$MODEL_ID" --host 0.0.0.0 --port "$PORT" \
    --gpu-memory-utilization "$GPU_MEM_UTIL" \
    --max-model-len "$MAX_MODEL_LEN" \
    "${EXTRA_ARGS[@]}"
