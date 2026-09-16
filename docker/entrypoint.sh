#!/bin/bash
# 引擎容器入口: 同时拉起 HTTP 批量转写与 WebSocket 流式两个服务, 任一退出则整容器退出
# (交由 docker --restart 策略重建, 避免半存活状态)
set -euo pipefail

MODEL_ID="${MODEL_ID:-FunAudioLLM/Fun-ASR-Nano-2512}"
DEVICE="${DEVICE:-cuda:0}"
HTTP_DEVICE="${HTTP_DEVICE:-$DEVICE}"    # 显存紧张的小卡可设 cpu: 批量走 CPU (实测 4.2s 音频约 3s), 流式独占 GPU
HUB="${HUB:-ms}"                       # ms=ModelScope / hf=HuggingFace
LANGUAGE="${LANGUAGE:-日本語}"
DTYPE="${DTYPE:-fp32}"                  # T4 (sm75) 不支持 bf16; 引擎会把 fp16 静默映射成 bf16, 只能 fp32
WS_GPU_MEM_UTIL="${WS_GPU_MEM_UTIL:-0.55}"
HTTP_PORT="${HTTP_PORT:-9001}"
WS_PORT="${WS_PORT:-10095}"

# funasr-server 的 --model 只接受别名 (fun-asr-nano 等), 模型 ID 要走 --model-path;
# 其内置 vLLM 尝试在无 bf16 的卡上必崩且残留显存, 默认禁用 (镜像内补丁);
# bf16 卡显式 -e FUNASR_SERVER_NO_VLLM= (置空) 即可恢复批量 vLLM
if [ -z "${FUNASR_SERVER_NO_VLLM+x}" ]; then
    export FUNASR_SERVER_NO_VLLM=1
fi
echo "[engine] HTTP 批量转写: funasr-server model-path=$MODEL_ID device=$HTTP_DEVICE hub=$HUB port=$HTTP_PORT"
funasr-server --model-path "$MODEL_ID" --device "$HTTP_DEVICE" --hub "$HUB" \
    --host 0.0.0.0 --port "$HTTP_PORT" &
HTTP_PID=$!

# 等批量服务就绪 (AutoModel 常驻显存) 后再起 WS: vLLM 按"当前剩余显存"配比,
# 两服务并发启动会互相看不见对方而超发, 实测会把后加载的一方挤到 OOM
echo "[engine] 等待 HTTP 批量服务就绪..."
READY=0
for _ in $(seq 1 120); do
    if python -c "
import urllib.request, urllib.error, sys
try:
    urllib.request.urlopen('http://127.0.0.1:$HTTP_PORT/v1/models', timeout=3)
except urllib.error.HTTPError:
    pass
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        READY=1
        break
    fi
    if ! kill -0 "$HTTP_PID" 2>/dev/null; then
        echo "[engine] funasr-server 启动阶段即退出" >&2
        exit 1
    fi
    sleep 5
done
# 探测用尽后不得继续：半残状态下 WS 照样拉起，日志还在撒谎说"已就绪"；
# 非零退出交给 docker --restart 策略重建
if [ "$READY" -ne 1 ]; then
    echo "[engine] 等待 HTTP 批量服务就绪超时（120 次探测，约 10 分钟），放弃启动" >&2
    exit 1
fi
echo "[engine] HTTP 批量服务已就绪"

# --enforce-eager: 不捕获 CUDA graph, 与批量服务共用一张卡时省显存
echo "[engine] WS 流式识别: funasr-realtime-server model=$MODEL_ID port=$WS_PORT" \
    "language=$LANGUAGE dtype=$DTYPE gpu-mem-util=$WS_GPU_MEM_UTIL"
funasr-realtime-server --model "$MODEL_ID" --hub "$HUB" --device "$DEVICE" \
    --port "$WS_PORT" --language "$LANGUAGE" --dtype "$DTYPE" \
    --gpu-memory-utilization "$WS_GPU_MEM_UTIL" --enforce-eager &
WS_PID=$!

# 任一子进程退出: 打出是谁挂了, 杀掉另一个后以非零退出 (触发容器重启)
wait -n "$HTTP_PID" "$WS_PID" || true
if ! kill -0 "$HTTP_PID" 2>/dev/null; then
    echo "[engine] funasr-server (pid $HTTP_PID) 已退出" >&2
fi
if ! kill -0 "$WS_PID" 2>/dev/null; then
    echo "[engine] funasr-realtime-server (pid $WS_PID) 已退出" >&2
fi
kill "$HTTP_PID" "$WS_PID" 2>/dev/null || true
exit 1
