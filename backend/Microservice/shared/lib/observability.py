"""
Phase 3 - Observability.

Lightweight, dependency-free request tracing so a single request can be followed
as it flows gateway -> service -> service. Adds:
  - X-Request-ID (reuses the gateway's header if present, else generates one)
  - X-Service-Name on every response
  - a structured one-line access log per request with duration

If OpenTelemetry is installed and OTEL_EXPORTER_OTLP_ENDPOINT is set, spans are
also exported; otherwise this stays a no-op beyond the header + log.
"""
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)


def install_observability(app, service_name: str) -> None:
    log = logging.getLogger(service_name)

    class _Middleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
            start = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception:
                dur = (time.perf_counter() - start) * 1000
                log.exception(
                    'req_id=%s method=%s path=%s status=500 dur_ms=%.1f',
                    req_id, request.method, request.url.path, dur,
                )
                raise
            dur = (time.perf_counter() - start) * 1000
            response.headers["X-Request-ID"] = req_id
            response.headers["X-Service-Name"] = service_name
            log.info(
                'req_id=%s method=%s path=%s status=%s dur_ms=%.1f',
                req_id, request.method, request.url.path, response.status_code, dur,
            )
            return response

    app.add_middleware(_Middleware)

    # Optional OpenTelemetry auto-instrumentation (only if the libs are present).
    try:
        import os
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor.instrument_app(app)
    except Exception:  # pragma: no cover
        pass
