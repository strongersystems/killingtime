FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KT_DB_PATH=/data/killingtime.db KT_HOST=0.0.0.0

WORKDIR /app
COPY pyproject.toml README.md ./
COPY killingtime ./killingtime
RUN pip install --no-cache-dir . && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

# Incremental sync every 60 minutes while serving.
CMD ["kt", "serve", "--host", "0.0.0.0", "--port", "8000", "--sync-every", "60"]
