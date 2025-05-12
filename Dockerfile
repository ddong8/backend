# backend/Dockerfile

# --- 阶段 1: 构建基础和安装依赖 ---
# 使用官方 Python 镜像的 slim-buster 版本作为基础，它体积较小且包含常用工具
FROM python:3.10-slim-buster AS python-base

# 设置环境变量，确保 Python 输出不被缓冲并直接打印到控制台，且不生成 .pyc 文件
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 设置 APT 非交互模式，避免在构建过程中出现提示
ENV DEBIAN_FRONTEND=noninteractive

# 更新 apt 包列表并安装一些构建 Python 包可能需要的系统依赖
# build-essential: 包含 gcc, g++, make 等编译工具
# libpq-dev: PostgreSQL 客户端开发库，psycopg2-binary 可能需要它 (尽管 binary 通常自带)
# netcat-openbsd: (可选) 可以用于在启动脚本中测试数据库连接是否就绪 (例如 wait-for-it.sh)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       libpq-dev \
    #    netcat-openbsd \ # 如果需要 nc 命令
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# --- 阶段 2: 创建应用用户和工作目录 ---
# 创建一个非 root 用户来运行应用，以增强安全性
RUN groupadd --gid 1001 appuser && \
    useradd --uid 1001 --gid 1001 --shell /bin/bash --create-home appuser

# 设置工作目录
WORKDIR /app

# 将 PYTHONPATH 设置为 /app，这样可以直接 import backend.main 等模块
ENV PYTHONPATH=/app

# --- 阶段 3: 安装 Python 应用依赖 ---
# 复制依赖描述文件
COPY requirements.txt .

# 安装 Python 依赖包
# --no-cache-dir: 不保留下载缓存，减小镜像体积
# --prefer-binary: 尽可能使用预编译的
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# --- 阶段 4: 复制应用代码并设置权限 ---
# 将当前目录 (构建上下文中的 backend/ 目录) 的所有内容复制到镜像的 /app 目录
# .dockerignore 文件会排除不需要复制的文件
COPY . .

# 创建日志目录 (如果 Loguru 配置了文件输出到 logs/)，并赋予 appuser 权限
# 确保容器内的 appuser 对此目录有写权限
RUN mkdir -p /app/logs && chown -R appuser:appuser /app/logs

# 将 /app 目录及其所有内容的所有权更改为 appuser
RUN chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# --- 阶段 5: 暴露端口和定义启动命令 ---
# 暴露 FastAPI 应用在容器内部运行的端口 (例如 8000)
# 这并不实际将端口发布到主机，发布操作在 docker-compose.yml 中定义
EXPOSE 8000

# 默认的容器启动命令在 docker-compose.yml 文件中通过 `command` 指令指定。
# 这样做的好处是可以在不同的环境 (开发/生产) 中使用不同的启动命令，
# 而无需修改 Dockerfile 并重新构建镜像。
# 例如，生产环境可能使用 Gunicorn:
# CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:8000", "backend.main:app"]
# 开发环境可能使用 Uvicorn 带热重载 (如果卷已挂载):
# CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# Dockerfile 可以有一个默认的 CMD，但 docker-compose 中的 command 会覆盖它。
# 提供一个简单的默认 CMD 作为回退或用于直接运行 Docker 镜像的场景。
CMD ["python", "-m", "backend.main"] # 这会尝试运行 backend/main.py 中的 if __name__ == "__main__": 块