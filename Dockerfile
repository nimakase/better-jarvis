# 贾维斯容器镜像
FROM python:3.11-slim

WORKDIR /app
COPY . /app

# 安装核心依赖（editable 方式，使 frontend/ 等资源路径仍指向 /app）
RUN pip install --no-cache-dir -e .

# 容器内须对外监听；并显式提供加密密钥（容器无系统钥匙串）
ENV JARVIS_HOST=0.0.0.0 \
    JARVIS_PORT=8000

EXPOSE 8000

# 运行须提供：OPENROUTER_API_KEY、MEMORY_ENCRYPTION_KEY（见 分发与部署.md）
CMD ["jarvis"]
