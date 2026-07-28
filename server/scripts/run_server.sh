#!/bin/bash
# ChaosOps Server 本地开发启动脚本
# 使用前请确保已创建虚拟环境并安装依赖：
#   python -m venv .venv
#   source .venv/bin/activate
#   pip install -r requirements.txt

set -e

cd "$(dirname "$0")/.."

# 加载本地 .env 文件（如果存在）
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# 开发环境默认值
export DEBUG="${DEBUG:-true}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///./chaosops.db}"
export SECRET_KEY="${SECRET_KEY:-dev-secret-key-change-in-production}"

uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
