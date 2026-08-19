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

# 先启动主服务（后台），Agent 依赖它提供提示词/知识库/元数据
echo "[*] 启动主服务 (8900 端口)..."
cd "$BACKEND_DIR"
python3 main.py &
MAIN_PID=$!
echo "    主服务 PID: $MAIN_PID"

# 等待主服务就绪，避免 Agent 启动时预加载报 Connection refused
echo "[*] 等待主服务就绪..."
for i in $(seq 1 30); do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:8900/api/v1/meta"; then
        echo "    主服务已就绪 (${i}s)"
        break
    fi
    if ! kill -0 $MAIN_PID 2>/dev/null; then
        echo "    [!] 主服务启动失败，请检查日志"
        exit 1
    fi
    sleep 1
    [ "$i" = "30" ] && echo "    [!] 等待超时，Agent 仍会启动（预加载可能失败，会自动重试）"
done

# 再启动 Agent 服务（前台）
echo "[*] 启动 Agent 服务 (8901 端口)..."
cd "$AGENT_DIR"
export PROMPT_DB_PATH="../backend/prompt_manager.db"
echo ""
echo "    主服务地址: http://0.0.0.0:8900"
echo "    Agent 服务: http://0.0.0.0:8901"
echo "    API文档:    http://0.0.0.0:8900/docs"
echo ""
echo "    按 Ctrl+C 停止所有服务"
echo "============================================"

# 捕获退出信号，同时杀掉主服务进程
cleanup() {
    echo ""
    echo "[*] 停止所有服务..."
    kill $MAIN_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

python3 main.py
