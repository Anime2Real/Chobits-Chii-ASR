#!/bin/bash
# 启动小叽 ASR 门面 (tools/server.py: 鉴权 + 限流 + OpenAI 垫片 + 流式 WS 网关)
# 用法: bash tools/start_asr_api.sh [端口, 默认 9881]
# 需要环境变量 CHII_ASR_API_KEY (systemd 从 /etc/chobits-chii-asr.env 读取);
# 首次运行自动在仓库根目录建 .venv 并安装 requirements.txt
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-9881}"
VENV="$REPO_ROOT/.venv"

if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install -r "$REPO_ROOT/requirements.txt"
fi

exec "$VENV/bin/python" "$REPO_ROOT/tools/server.py" "$PORT"
