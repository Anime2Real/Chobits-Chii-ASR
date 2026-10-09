#!/bin/bash
# Qwen3-ASR 引擎容器入口: 单进程 HTTP 批量转写 (qwen-asr-serve, OpenAI 兼容, vLLM)
# 无 WS 流式服务 (官方 SDK 级流式未包装为服务端点, 见 README Roadmap)
set -euo pipefail

MODEL_ID="${MODEL_ID:-Qwen/Qwen3-ASR-0.6B}"
PORT="${PORT:-8000}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.8}"

echo "[engine] HTTP 批量转写: qwen-asr-serve model=$MODEL_ID port=$PORT gpu-mem-util=$GPU_MEM_UTIL"
# exec 为 PID 1: docker stop 的 TERM 直达 qwen-asr-serve, 由 vLLM 完成优雅卸载。
# --host/--port/--gpu-memory-utilization 均透传给底层 vllm serve
exec qwen-asr-serve "$MODEL_ID" --host 0.0.0.0 --port "$PORT" \
    --gpu-memory-utilization "$GPU_MEM_UTIL"
