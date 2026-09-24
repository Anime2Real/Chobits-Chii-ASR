#!/bin/bash
# 启动小叽 ASR 门面 (tools/server.py: 鉴权 + 限流 + OpenAI 垫片 + 流式 WS 网关)
# 用法: bash tools/start_asr_api.sh [端口, 默认 9881]
# 需要环境变量 CHII_ASR_API_KEY (systemd 从 /etc/chobits-chii-asr.env 读取);
# 首次运行自动在仓库根目录建 .venv 并安装共享库 + requirements.txt
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-9881}"
VENV="$REPO_ROOT/.venv"
# 门面公共逻辑共享库（chii_facade_common）源在兄弟仓库 Chobits-Chii-ServerDeploy，
# 生产 /home/ubuntu/Github/ 下三仓库互为兄弟目录；兼容更名前的旧目录名 CloudDeploy
COMMON_LIB=""
for _sibling in "$REPO_ROOT/../Chobits-Chii-ServerDeploy" "$REPO_ROOT/../Chobits-Chii-CloudDeploy"; do
    if [ -d "$_sibling/tools/chii-facade-common" ]; then
        COMMON_LIB="$_sibling/tools/chii-facade-common"
        break
    fi
done

install_common_lib() {
    if [ ! -d "$COMMON_LIB" ]; then
        echo "[错误] 未找到门面共享库: $REPO_ROOT/../Chobits-Chii-ServerDeploy/tools/chii-facade-common" >&2
        echo "       本门面依赖兄弟仓库的共享库，请先同级 clone Chobits-Chii-ServerDeploy 后重试，" >&2
        echo "       或手动安装: pip install -e <chii-facade-common 路径>" >&2
        exit 1
    fi
    "$VENV/bin/pip" install -e "$COMMON_LIB"
}

if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
    install_common_lib
    "$VENV/bin/pip" install -r "$REPO_ROOT/requirements.txt"
elif ! "$VENV/bin/python" -c "import chii_facade_common" >/dev/null 2>&1; then
    # 旧 venv 补装共享库（共享库接入前已存在的门面环境）
    install_common_lib
fi

exec "$VENV/bin/python" "$REPO_ROOT/tools/server.py" "$PORT"
