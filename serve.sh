#!/bin/bash
# 一键启动「贾维斯 + Cloudflare 隧道」。
# 两个进程一起拉起，Ctrl+C 一起关。配置见 deploy/cloudflared.yml。

cd "$(dirname "$0")"
CONFIG="$(pwd)/deploy/cloudflared.yml"
PY="$(pwd)/.venv/bin/python"

# 检查 .env
if [ ! -f .env ]; then
    echo "缺少 .env，请先复制 .env.example 并填好密钥。"
    exit 1
fi

echo "启动贾维斯后端 (127.0.0.1:8000) ..."
"$PY" main.py &
JARVIS_PID=$!

# 退出时一并关闭后端
cleanup() {
    echo ""
    echo "正在关闭贾维斯 ..."
    kill "$JARVIS_PID" 2>/dev/null
    exit 0
}
trap cleanup INT TERM

# 等后端起来再开隧道
sleep 2
echo "启动 Cloudflare 隧道 (jarvis.hks0110.com) ..."
cloudflared tunnel --config "$CONFIG" run jarvis

# 隧道退出后也关掉后端
cleanup
