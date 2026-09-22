FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
EXPOSE 8080
# 持久化：在 Fly.io 中配合 fly.toml 挂载 volume 到 /data，并设置 KB_DATA_DIR=/data、KB_UPLOAD_DIR=/data/uploads
CMD ["sh", "-c", "waitress-serve --port=${PORT:-8080} --call app:app"]
