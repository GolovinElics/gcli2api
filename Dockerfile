# 基础镜像
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
WORKDIR /app
COPY . .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --upgrade pip && \
    python -m pip install --no-cache-dir -i "$PIP_INDEX_URL" fastapi hypercorn redis toml aiofiles 'httpx[socks]' python-dotenv motor asyncpg
EXPOSE 7861
CMD ["python", "web.py"]
