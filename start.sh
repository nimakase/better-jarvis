#!/bin/bash
# 一键启动贾维斯——不用再手动 source .venv/bin/activate。
# 没有虚拟环境会自动建、没装依赖会自动装，直接跑这一个脚本就够了。
set -e
cd "$(dirname "$0")"

echo "启动贾维斯..."

# 检查 .env
if [ ! -f .env ]; then
    echo "未找到 .env，正在从模板复制..."
    cp .env.example .env
    echo "请先编辑 .env 填入你的 API Key，然后重新运行此脚本。"
    exit 1
fi

# 虚拟环境：没有就建（此后每次直接用 .venv/bin/python，不需要手动 activate）
VENV_DIR=".venv"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "未找到虚拟环境，正在创建..."
    python3 -m venv "$VENV_DIR"
fi
PY="$VENV_DIR/bin/python"

# 安装依赖（首次运行；用项目实际依赖 openai 做探测，装过就跳过）
if ! "$PY" -c "import openai" 2>/dev/null; then
    echo "安装依赖..."
    "$PY" -m pip install -r requirements.txt
fi

# 启动
echo "服务启动中，请在浏览器打开 http://localhost:8000"
exec "$PY" main.py
