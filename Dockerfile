# 基础镜像
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN python -m pip install --upgrade pip && python -m pip install --no-cache-dir .
EXPOSE 7861
CMD ["python", "web.py"]