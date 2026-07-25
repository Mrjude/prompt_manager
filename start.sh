#!/bin/bash
# 提示词管理系统 - 一键启动脚本（同时启动主服务 + Agent 服务）

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"
AGENT_DIR="$SCRIPT_DIR/agent"

echo "============================================"
echo "  提示词管理系统 + Agent 服务 启动"
echo "============================================"

# 检查依赖
cd "$BACKEND_DIR"
if ! python3 -c "import fastapi, uvicorn" 2>/dev/null; then
    echo "[*] 安装依赖..."
    pip3 install -q fastapi uvicorn pydantic
fi

# 启动 Agent 服务（后台）
echo "[*] 启动 Agent 服务 (8901 端口)..."
cd "$AGENT_DIR"
export PROMPT_DB_PATH="../backend/prompt_manager.db"
python3 main.py &
AGENT_PID=$!
echo "    Agent PID: $AGENT_PID"

# 启动主服务（前台）
echo "[*] 启动主服务 (8900 端口)..."
cd "$BACKEND_DIR"
echo ""
echo "    主服务地址: http://0.0.0.0:8900"
echo "    Agent 服务: http://0.0.0.0:8901"
echo "    API文档:    http://0.0.0.0:8900/docs"
echo ""
echo "    按 Ctrl+C 停止所有服务"
echo "============================================"

# 捕获退出信号，同时杀掉 Agent 进程
cleanup() {
    echo ""
    echo "[*] 停止所有服务..."
    kill $AGENT_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

python3 main.py
