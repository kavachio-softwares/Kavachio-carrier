FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app:/app/shared/lib

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl gcc libpq-dev nginx \
    && rm -rf /var/lib/apt/lists/*

COPY services /app/services
COPY shared /app/shared
COPY gateway /app/gateway
COPY start.sh /start.sh
COPY gateway/nginx.containerapp.conf /etc/nginx/nginx.conf

RUN chmod +x /start.sh \
    && for req in /app/services/*/requirements.txt; do [ -f "$req" ] && pip install --no-cache-dir -r "$req"; done

EXPOSE 80

ENTRYPOINT ["/start.sh"]
