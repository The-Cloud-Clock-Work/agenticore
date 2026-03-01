# Auth Broker — FastAPI service
# python:3.13-slim + uvicorn, no Claude CLI needed
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/The-Cloud-Clock-Work/agenticore"
LABEL org.opencontainers.image.description="Auth Broker — human-in-the-loop OAuth token gateway"
LABEL org.opencontainers.image.licenses="MIT"

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir --upgrade pip && \
    pip uninstall -y setuptools 2>/dev/null; true

WORKDIR /app

COPY auth_broker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip uninstall -y pip 2>/dev/null; true

COPY auth_broker/ auth_broker/

# Default providers.yaml — overridden by ConfigMap volume mount in K8s
COPY auth_broker/providers.yaml /app/providers.yaml

RUN useradd -m -s /bin/sh authbroker && \
    chown -R authbroker:authbroker /app

ENV AUTH_BROKER_HOST=0.0.0.0 \
    AUTH_BROKER_PORT=8300 \
    PROVIDERS_PATH=/app/providers.yaml

USER authbroker

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8300/health || exit 1

CMD ["uvicorn", "auth_broker.main:app", "--host", "0.0.0.0", "--port", "8300"]
