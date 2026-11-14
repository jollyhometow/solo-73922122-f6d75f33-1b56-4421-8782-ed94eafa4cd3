FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 进程以非 root 用户运行
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# 单 API 进程；数据库连接池上限由 API_POOL_MAX 控制（默认 2）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
