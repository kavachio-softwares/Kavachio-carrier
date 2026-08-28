#!/bin/sh
set -eu

export PYTHONPATH=/app:/app/shared/lib

start_service() {
  service_name="$1"
  port="$2"
  (
    cd "/app/services/$service_name"
    echo "Starting $service_name on port $port"
    PYTHONPATH=/app:/app/shared/lib uvicorn app.main:app --host 0.0.0.0 --port "$port"
  ) &
}

start_service auth 8001
start_service tenant-admin 8007
start_service mapper 8002
start_service ingestion 8003
start_service validation 8004
start_service export 8005
start_service contract 8006

exec nginx -g 'daemon off;'
