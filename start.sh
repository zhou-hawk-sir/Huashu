#!/usr/bin/env bash
# OpsAgent 后端前台启动脚本（开发/调试用）
# 自动检测 /home/vmuser/opsagent-venv 共享 venv；不存在则创建
# 用法：bash start.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

# 共享环境（跨 RJB_zhou2.0 / RJB_zhou3.0 复用）
VENV_DIR="/home/vmuser/opsagent-venv"
LOG_DIR="/home/vmuser/opsagent-logs"
DATA_DIR="/home/vmuser/opsagent-data"

mkdir -p "$LOG_DIR" "$DATA_DIR"

# 1. venv
if [ ! -d "$VENV_DIR" ]; then
    echo "[start.sh] 创建共享 venv: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 2. 装依赖（首次 / 跨版本升级时）
echo "[start.sh] 同步依赖到 $VENV_DIR（首次 ~60s）..."
pip install --quiet --upgrade pip
pip install --quiet -r "$BACKEND_DIR/requirements.txt"

# 3. 启动
echo "[start.sh] 启动 OpsAgent（监听 0.0.0.0:8080，按 Ctrl+C 停止）..."
echo "[start.sh] 日志目录: $LOG_DIR"
echo "[start.sh] 数据目录: $DATA_DIR"
cd "$BACKEND_DIR"
export OPSAGENT_DATA_DIR="$DATA_DIR"
export OPSAGENT_LOG_DIR="$LOG_DIR"
exec python main.py
