# 使用官方轻量级 Python 3.11 镜像作为基础
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（如果需要）
# RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# 将依赖文件复制到镜像中
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 将项目所有文件复制到镜像的工作目录
COPY . .

# 创建数据缓存目录
RUN mkdir -p data_cache

# 声明容器运行时监听的端口（Streamlit 默认 8501）
EXPOSE 8501

# 容器启动时运行的命令
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]