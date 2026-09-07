FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KT_DB_PATH=/data/killingtime.db KT_HOST=0.0.0.0

WORKDIR /app
COPY pyproject.toml README.md ./
COPY killingtime ./killingtime
# The optional "ca" build secret lets sandboxed CI builds trust a TLS-inspecting proxy; it is absent in normal builds.
RUN --mount=type=secret,id=ca,target=/tmp/ca.crt,required=false \
    sh -c 'if [ -s /tmp/ca.crt ]; then export PIP_CERT=/tmp/ca.crt; fi; pip install --no-cache-dir .' \
    && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000

# Set KT_SYNC_EVERY=60 to run an in-process incremental sync on a schedule (docker-compose does).
CMD ["sh", "-c", "kt serve --host 0.0.0.0 --port 8000 ${KT_SYNC_EVERY:+--sync-every $KT_SYNC_EVERY}"]
