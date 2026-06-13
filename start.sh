#!/bin/bash
echo "启动贾维斯..."

# 检查 .env
if [ ! -f .env ]; then
    echo "未找到 .env，正在从模板复制..."
    cp .env.example .env
    echo "请先编辑 .env 填入你的 API Key，然后重新运行此脚本。"
    exit 1
fi

# 安装依赖（首次运行）
if ! python -c "import anthropic" 2>/dev/null; then
    echo "安装依赖..."
    pip install -r requirements.txt
fi

# 启动并打开浏览器
echo "服务启动中，请在浏览器打开 http://localhost:8000"
if command -v open &>/dev/null; then
    sleep 1 && open http://localhost:8000 &
fi
python main.py
