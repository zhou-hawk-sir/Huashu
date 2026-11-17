#!/usr/bin/env bash
# OpsAgent 一键部署脚本（麒麟 V11 + loongarch64 + 普通用户 vmuser）
#
# 目录布局：
#   /home/vmuser/opsagent-venv       共享 venv（跨 RJB_zhou2.0/3.0 复用）
#   /home/vmuser/opsagent-logs       共享日志目录
#   /home/vmuser/opsagent-data       共享运行时数据（audit.db）
#   /home/vmuser/RJB_zhou2.0/        当前版本项目（用户上传）
#   /etc/systemd/system/opsagent.service   systemd 单元
#   /etc/nginx/conf.d/opsagent.conf        nginx 配置
#
# 用法：
#   1. 上传 RJB_zhou2.0 到 /home/vmuser/RJB_zhou2.0
#   2. cd /home/vmuser/RJB_zhou2.0 && sudo bash deploy.sh
set -e

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log() { echo -e "${GREEN}[deploy]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }

# 0. 权限
if [ "$EUID" -ne 0 ]; then
    warn "请用 sudo 运行：sudo bash deploy.sh"
    exit 1
fi

# 路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
VENV_DIR="/home/vmuser/opsagent-venv"
LOG_DIR="/home/vmuser/opsagent-logs"
DATA_DIR="/home/vmuser/opsagent-data"
SERVICE_NAME="opsagent"
SERVICE_USER="vmuser"
LOG_FILE="/var/log/${SERVICE_NAME}/app.log"

# 1. Python 版本
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
    warn "未找到 python3，先安装：sudo dnf install -y python3 python3-pip python3-venv"
    exit 1
fi
PYVER=$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "Python: $PYVER"
case "$PYVER" in
    3.1[0-9]) ;;  # OK
    *)
        warn "Python ≥ 3.10 建议（当前 $PYVER）"
        ;;
esac

# 2. 共享目录（环境已就绪，跳过）
# log "创建共享目录 /home/vmuser/opsagent-{venv,logs,data}"
# mkdir -p "$VENV_DIR" "$LOG_DIR" "$DATA_DIR"
# chown -R "${SERVICE_USER}:${SERVICE_USER}" /home/vmuser/opsagent-venv /home/vmuser/opsagent-logs /home/vmuser/opsagent-data
log "复用现有目录（已存在）：$VENV_DIR / $LOG_DIR / $DATA_DIR"

# 3. venv + 依赖（环境已就绪，跳过）
# if [ ! -f "$VENV_DIR/bin/activate" ]; then
#     log "创建共享 venv: $VENV_DIR"
#     sudo -u "$SERVICE_USER" "$PYTHON" -m venv "$VENV_DIR"
# fi
# sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet --upgrade pip
# log "同步依赖到 venv（首次 1-2 分钟）..."
# sudo -u "$SERVICE_USER" "$VENV_DIR/bin/pip" install --quiet -r "$BACKEND_DIR/requirements.txt"
log "复用现有 venv + 依赖：$VENV_DIR"

# 4. systemd unit
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${DATA_DIR}/opsagent.env"

# 4.0 LLM 密钥环境文件：不存在则生成模板并告警（避免静默回退 MockClient）
if [ ! -f "$ENV_FILE" ]; then
    warn "未找到 LLM 密钥文件：$ENV_FILE，已生成模板，请填入真实 DEEPSEEK_API_KEY："
    cat > "$ENV_FILE" <<'ENVEOF'
# OpsAgent 运行时环境变量（systemd EnvironmentFile 加载）
# 填入真实 DeepSeek API Key，否则后端回退 MockClient（不会真正调用大模型）
DEEPSEEK_API_KEY=
ENVEOF
    chown "${SERVICE_USER}:${SERVICE_USER}" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    warn "  编辑：sudo -u ${SERVICE_USER} vi $ENV_FILE  然后重跑 deploy.sh"
elif ! grep -qE '^DEEPSEEK_API_KEY=.+' "$ENV_FILE"; then
    warn "⚠️  $ENV_FILE 中 DEEPSEEK_API_KEY 为空，后端会回退 MockClient（不真正调用大模型）"
fi

log "写入 systemd unit: $SERVICE_FILE"
mkdir -p /var/log/${SERVICE_NAME}
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=OpsAgent · 麒麟智能运维 Agent (FastAPI + WebSocket)
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${BACKEND_DIR}
Environment="PATH=${VENV_DIR}/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
Environment="OPSAGENT_DATA_DIR=${DATA_DIR}"
Environment="OPSAGENT_LOG_DIR=${LOG_DIR}"
EnvironmentFile=-${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python main.py
Restart=always
RestartSec=3
StandardOutput=append:${LOG_FILE}
StandardError=append:${LOG_FILE}

[Install]
WantedBy=multi-user.target
EOF

# 5. 启动
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

# 6. 验证（轮询重试：LoongArch 冷启动要跑环境探测+知识库+hooks，可能 >10s）
log "验证后端（最长等待 40s）..."
HEALTH_OK=0
for i in $(seq 1 20); do
    # 服务若已崩溃退出，立刻停止等待
    if ! systemctl is-active --quiet ${SERVICE_NAME}; then
        warn "服务未处于 active 状态（第 ${i} 次探测），疑似启动即退出/崩溃"
        break
    fi
    if curl -fs http://localhost:8080/api/health >/dev/null 2>&1; then
        HEALTH_OK=1
        log "✅ 后端启动成功（等待约 $((i*2))s，http://localhost:8080/api/health）"
        break
    fi
    sleep 2
done

if [ "$HEALTH_OK" -ne 1 ]; then
    warn "后端健康检查未通过，以下为现场诊断："
    echo "----- systemctl status -----"
    systemctl status ${SERVICE_NAME} --no-pager -l | tail -20 || true
    echo "----- journalctl (最近 40 行) -----"
    journalctl -u ${SERVICE_NAME} -n 40 --no-pager || true
    echo "----- ${LOG_FILE} (最近 40 行) -----"
    tail -40 "$LOG_FILE" 2>/dev/null || true
    warn "常见排查："
    warn "  1) 密钥缺失只会回退 Mock，不影响启动；若反复重启看上面 journal 的报错行"
    warn "  2) 手动以 systemd 相同环境前台复现真实报错："
    warn "     cd ${BACKEND_DIR} && sudo -u ${SERVICE_USER} \\"
    warn "       OPSAGENT_DATA_DIR=${DATA_DIR} OPSAGENT_LOG_DIR=${LOG_DIR} \\"
    warn "       ${VENV_DIR}/bin/python main.py"
    exit 1
fi

# 7. 提示 nginx
cat <<EOF

${GREEN}✅ 部署完成${NC}

下一步（前端 nginx）:
  sudo cp ${SCRIPT_DIR}/nginx.conf.example /etc/nginx/conf.d/opsagent.conf
  sudo nginx -t && sudo nginx -s reload

浏览器访问: http://<your-host>:8081

后续版本升级（如 RJB_zhou3.0）:
  1. 上传新版本到 /home/vmuser/RJB_zhou3.0
  2. cd /home/vmuser/RJB_zhou3.0 && sudo bash deploy.sh
     （脚本会复用现有 venv + 依赖，不重新创建环境）
EOF
