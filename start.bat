@echo off
echo 启动贾维斯...

REM 检查 .env
if not exist .env (
    echo 未找到 .env，正在从模板复制...
    copy .env.example .env
    echo 请先编辑 .env 填入你的 API Key，然后重新运行此脚本。
    pause
    exit /b 1
)

REM 安装依赖（首次运行）
pip show anthropic >nul 2>&1
if errorlevel 1 (
    echo 安装依赖...
    pip install -r requirements.txt
)

REM 启动
echo 服务启动中，请在浏览器打开 http://localhost:8000
start http://localhost:8000
python main.py
