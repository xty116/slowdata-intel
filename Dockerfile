# 慢数据情报台 —— 容器化部署（Render / Railway / Fly.io 通用）
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY slowdata ./slowdata
COPY config ./config

RUN pip install --no-cache-dir -e .

ENV HF_HOME=/data/.hf-cache \
    FASTEMBED_CACHE_PATH=/data/.fastembed-cache \
    SLOWDATA_NO_OPEN=1

EXPOSE 8000

CMD ["python", "-m", "slowdata", "serve", "--host", "0.0.0.0"]
