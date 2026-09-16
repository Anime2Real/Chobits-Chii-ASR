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

# docker stop 只给容器 PID 1 发 TERM/INT, 直接转发给两个引擎子进程。pid 先置空:
# trap 可能在任一子进程拉起前触发 (如就绪等待期收到 TERM), 空值按"已退出"处理
HTTP_PID=""
WS_PID=""

child_alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }

grace_stop() {
    # 优雅停止: 先 TERM 两个引擎子进程并最多等 30s (模型卸载/连接 drain),
    # 仍活着再 KILL 兜底; 以子进程退出码收尾 (0 = 均已优雅退出, 非零透传残态)。
    # trap 在 set -e 下执行: 每个可能失败的命令都要 || true, 否则半路 abort
    trap - TERM INT
    echo "[engine] 收到 TERM/INT，优雅停止两个引擎子进程..." >&2
    kill -TERM $HTTP_PID $WS_PID 2>/dev/null || true
    for _ in $(seq 1 30); do
        if ! child_alive "$HTTP_PID" && ! child_alive "$WS_PID"; then
            break
        fi
        sleep 1
    done
    kill -9 $HTTP_PID $WS_PID 2>/dev/null || true
    rc=0
    if [ -n "$HTTP_PID" ]; then
        wait "$HTTP_PID" 2>/dev/null || rc=$?
    fi
    if [ -n "$WS_PID" ]; then
        wait "$WS_PID" 2>/dev/null || rc=$?
    fi
    echo "[engine] 引擎子进程已全部退出 (rc=$rc)" >&2
    exit "$rc"
}
trap grace_stop TERM INT

echo "[engine] HTTP 批量转写: funasr-server model-path=$MODEL_ID device=$HTTP_DEVICE hub=$HUB port=$HTTP_PORT"
funasr-server --model-path "$MODEL_ID" --device "$HTTP_DEVICE" --hub "$HUB" \
    --host 0.0.0.0 --port "$HTTP_PORT" &
HTTP_PID=$!

# 等批量服务就绪 (AutoModel 常驻显存) 后再起 WS: vLLM 按"当前剩余显存"配比,
# 两服务并发启动会互相看不见对方而超发, 实测会把后加载的一方挤到 OOM
echo "[engine] 等待 HTTP 批量服务就绪..."
READY=0
for _ in $(seq 1 120); do
    # 就绪判定 = HTTP 200 且响应体含引擎注册模型名特征串。funasr-server --model-path
    # 模式注册名为 "custom"，就绪时 /v1/models 返回 OpenAI 兼容
    # {"object":"list","data":[{"id":"custom",...}]}（依据 docs/deployment.md 与
    # tools/server.py ENGINE_MODEL 注释）；引擎残态吐的错误页/status 非 200 一律不算就绪。
    # urllib 对非 2xx 直接抛 HTTPError，这里再显式校验 status 与 body 双保险。
    if python -c "
import urllib.request, sys
try:
    with urllib.request.urlopen('http://127.0.0.1:$HTTP_PORT/v1/models', timeout=3) as resp:
        if resp.status != 200:
            sys.exit(1)
        body = resp.read().decode('utf-8', 'replace')
except Exception:
    sys.exit(1)
sys.exit(0 if 'custom' in body else 1)
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
