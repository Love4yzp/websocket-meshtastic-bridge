FROM python:3.10.16-slim-bullseye

WORKDIR /app

# 复制依赖文件并安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# 暴露 FastAPI 服务端口
EXPOSE 5800

# 启动 FastAPI 服务
CMD ["python","app.py"]
