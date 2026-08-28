"""
Inter-service HTTP client layer (full-decoupling foundation).

When one service needs a DOMAIN operation owned by another service (e.g. ingestion
needs the LLM mapping that mapper-service owns), it calls that service over HTTP
using the typed client here instead of importing the other service's engine module.

Service URLs come from env (set in docker-compose / k8s), defaulting to the local
compose network names. A request-id is propagated so calls can be traced end to end.

IMPORTANT - what does NOT belong here:
  Pure, stateless utilities (Excel parsing, glom spec application, signature hashing,
  warehouse reads) are NOT service couplings. They live in shared/lib as a normal
  shared library and every service imports them directly. Only stateful / domain
  operations (LLM mapping, rule validation, warehouse writes, document extraction,
  workbook generation) go over HTTP.
"""
from __future__ import annotations

import os
from contextvars import ContextVar

import httpx

# Propagated so a call chain shares one request id across services.
current_request_id: ContextVar[str | None] = ContextVar("current_request_id", default=None)

# Internal service base URLs (compose/k8s service names by default).
SERVICE_URLS = {
    "auth":         os.getenv("AUTH_URL",         "http://auth-service:8001"),
    "tenant-admin": os.getenv("TENANT_ADMIN_URL", "http://tenant-admin-service:8007"),
    "mapper":       os.getenv("MAPPER_URL",       "http://mapper-service:8002"),
    "ingestion":    os.getenv("INGESTION_URL",    "http://ingestion-service:8003"),
    "validation":   os.getenv("VALIDATION_URL",   "http://validation-service:8004"),
    "export":       os.getenv("EXPORT_URL",       "http://export-service:8005"),
    "contract":     os.getenv("CONTRACT_URL",     "http://contract-service:8006"),
}

DEFAULT_TIMEOUT = float(os.getenv("SERVICE_HTTP_TIMEOUT", "120"))


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    rid = current_request_id.get()
    if rid:
        h["X-Request-ID"] = rid
    tok = os.getenv("SERVICE_TOKEN")          # optional service-to-service auth
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def call(service: str, method: str, path: str, *, json=None, params=None,
         files=None, content=None, timeout: float | None = None) -> httpx.Response:
    """Low-level call to another service. Raises for HTTP errors.

    Pass `json` for simple payloads. Pass a dict via `json_safe(...)` -> `content`
    when the payload may hold non-JSON-native values (datetimes, Decimals)."""
    base = SERVICE_URLS[service]
    url = base.rstrip("/") + path
    headers = _headers()
    if files is not None:
        headers.pop("Content-Type", None)     # let httpx set multipart boundary
    with httpx.Client(timeout=timeout or DEFAULT_TIMEOUT) as client:
        resp = client.request(method, url, json=json, params=params,
                              files=files, content=content, headers=headers)
        resp.raise_for_status()
        return resp


def json_safe(payload: dict) -> bytes:
    """Serialize a payload tolerant of datetimes/Decimals (falls back to str())."""
    import json as _json
    return _json.dumps(payload, default=str).encode()


# --------------------------------------------------------------------------
# Typed domain operations. Each corresponds to an internal endpoint on the
# provider service (added as each boundary is converted). Until a provider
# endpoint exists, the consumer keeps its direct import; these are the target.
# --------------------------------------------------------------------------
def _df_to_json(df) -> dict:
    """Serialize a pandas DataFrame for transport. generate_mapping only reads
    column names + stringified samples, so a records round-trip is faithful."""
    return {
        "columns": [str(c) for c in df.columns],
        "rows": df.astype(object).where(df.notna(), None).values.tolist(),
    }


class MapperClient:
    @staticmethod
    def generate_mapping_df(sheets_dict) -> dict:
        """LLM mapping owned by mapper-service. `sheets_dict` maps sheet name ->
        pandas DataFrame. Provider: POST /internal/mapping/generate."""
        payload = {"sheets": {name: _df_to_json(df) for name, df in sheets_dict.items()}}
        return call("mapper", "POST", "/internal/mapping/generate", json=payload).json()


class IngestionClient:
    @staticmethod
    def ingest_records(upload_id: int, tenant_id: int) -> dict:
        """Warehouse write owned by ingestion-service. Provider: POST /internal/ingest."""
        return call("ingestion", "POST", "/internal/ingest",
                    json={"upload_id": upload_id, "tenant_id": tenant_id}).json()


class ValidationClient:
    @staticmethod
    def run_validation(records_by_sheet, rules, *, contract=None, template_id=None,
                       schema_cols=None, column_types=None) -> dict:
        """DuckDB rule validation owned by validation-service. Provider: POST /internal/validate."""
        payload = {
            "records_by_sheet": records_by_sheet,
            "rules": rules or [],
            "contract": contract,
            "template_id": template_id,
            "schema_cols": schema_cols,
            "column_types": column_types,
        }
        return call("validation", "POST", "/internal/validate",
                    content=json_safe(payload)).json()

    @staticmethod
    def label_exceptions(exceptions, structure, blocks) -> list:
        """label_exceptions_with_policy owned by validation-service. Returns labelled list."""
        payload = {"exceptions": exceptions, "structure": structure, "blocks": blocks}
        return call("validation", "POST", "/internal/label-exceptions",
                    content=json_safe(payload)).json()


class ExportClient:
    @staticmethod
    def parse_template(template_id: int) -> dict:
        """Template parsing owned by export-service. Provider: POST /internal/template/parse."""
        return call("export", "POST", "/internal/template/parse",
                    json={"template_id": template_id}).json()


class ContractClient:
    @staticmethod
    def extract_document(file_bytes: bytes, filename: str) -> dict:
        """Document extraction owned by contract-service. Provider: POST /internal/extract."""
        return call("contract", "POST", "/internal/extract",
                    files={"file": (filename, file_bytes)}).json()


mapper = MapperClient()
ingestion = IngestionClient()
validation = ValidationClient()
export = ExportClient()
contract = ContractClient()
