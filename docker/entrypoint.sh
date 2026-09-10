#!/bin/bash
# 引擎容器入口: 同时拉起 HTTP 批量转写与 WebSocket 流式两个服务, 任一退出则整容器退出
# (交由 docker --restart 策略重建, 避免半存活状态)
set -euo pipefail

MODEL_ID="${MODEL_ID:-FunAudioLLM/Fun-ASR-Nano-2512}"
DEVICE="${DEVICE:-cuda:0}"
HUB="${HUB:-ms}"                       # ms=ModelScope / hf=HuggingFace
LANGUAGE="${LANGUAGE:-中文}"
HTTP_PORT="${HTTP_PORT:-9001}"
WS_PORT="${WS_PORT:-10095}"

echo "[engine] HTTP 批量转写: funasr-server model=$MODEL_ID device=$DEVICE hub=$HUB port=$HTTP_PORT"
funasr-server --model "$MODEL_ID" --device "$DEVICE" --hub "$HUB" \
    --host 0.0.0.0 --port "$HTTP_PORT" &
HTTP_PID=$!

echo "[engine] WS 流式识别: serve_realtime_ws.py model=$MODEL_ID port=$WS_PORT language=$LANGUAGE"
python /app/serve_realtime_ws.py --model "$MODEL_ID" --device "$DEVICE" \
    --port "$WS_PORT" --language "$LANGUAGE" &
WS_PID=$!

# 任一子进程退出: 打出是谁挂了, 杀掉另一个后以非零退出 (触发容器重启)
wait -n "$HTTP_PID" "$WS_PID" || true
if ! kill -0 "$HTTP_PID" 2>/dev/null; then
    echo "[engine] funasr-server (pid $HTTP_PID) 已退出" >&2
fi
if ! kill -0 "$WS_PID" 2>/dev/null; then
    echo "[engine] serve_realtime_ws.py (pid $WS_PID) 已退出" >&2
fi
kill "$HTTP_PID" "$WS_PID" 2>/dev/null || true
exit 1
