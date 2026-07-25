#!/bin/bash
# Agent 服务启动脚本
cd "$(dirname "$0")"
export PROMPT_DB_PATH="../backend/prompt_manager.db"
python main.py "$@"
