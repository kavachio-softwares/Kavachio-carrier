"""storage.py — file/blob storage abstraction (Azurite locally, Azure Blob in prod).

One code path for both environments: only AZURE_STORAGE_CONNECTION_STRING changes
between local (Azurite emulator) and production (real Azure). When
STORAGE_BACKEND != "azure", callers fall back to the legacy LargeBinary DB
columns, so existing deployments keep working untouched.

Env is read LAZILY (at call time), not at import — this module is imported (via
app_routes) before main.py runs load_dotenv(), so reading at import would miss
the .env entirely.

Env:
  STORAGE_BACKEND                  "azure" -> blob storage; anything else -> DB blob (default)
  AZURE_STORAGE_CONNECTION_STRING  connection string (defaults to Azurite dev creds)
  AZURE_STORAGE_CONTAINER          container name (default "kavachio-uploads")
  AZURE_BLOB_PREFIX                optional virtual folder prefix inside the container
"""
import os
import uuid
import logging
from functools import lru_cache

_log = logging.getLogger("kavachio.storage")

# The Azure SDK logs a full dump of every HTTP request/response at INFO, which
# floods application logs. Quiet it to WARNING (errors still surface).
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)

# Azurite's canonical dev connection string — the default so a plain `azurite`
# on localhost needs zero config. NOT a secret (it's public and identical for
# every Azurite install); real accounts inject their own string via env.
_AZURITE_DEV_CONN = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)


# ── Config accessors (read env at call time, after .env is loaded) ───────────

def _backend() -> str:
    return os.getenv("STORAGE_BACKEND", "db").strip().lower()


def is_azure() -> bool:
    """True when blob storage is the active backend."""
    return _backend() == "azure"


def _conn_str() -> str:
    return os.getenv("AZURE_STORAGE_CONNECTION_STRING", _AZURITE_DEV_CONN)


def _container_name() -> str:
    return os.getenv("AZURE_STORAGE_CONTAINER", "kavachio-uploads")


def _prefix() -> str:
    return os.getenv("AZURE_BLOB_PREFIX", "").strip("/")   # configurable folder path


# ── Low-level blob I/O ──────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _container_client():
    """Cached container client. Creates the container on first use (idempotent).
    Cached so the connection string / container name are resolved once, on the
    first blob operation — which is well after load_dotenv() has run."""
    from azure.storage.blob import BlobServiceClient
    svc = BlobServiceClient.from_connection_string(_conn_str())
    cc = svc.get_container_client(_container_name())
    try:
        cc.create_container()          # first-run bootstrap; harmless if it exists
    except Exception:
        pass
    return cc


def build_key(category: str, tenant_id, filename: str) -> str:
    """<prefix>/<category>/<tenant>/<uuid>.<ext> — collision-free, tenant-scoped."""
    ext = os.path.splitext(filename or "")[1]
    parts = [_prefix(), category, str(tenant_id or "global"), f"{uuid.uuid4().hex}{ext}"]
    return "/".join(p for p in parts if p)


def put_bytes(key: str, data: bytes, content_type: str | None = None) -> str:
    from azure.storage.blob import ContentSettings
    cs = ContentSettings(content_type=content_type) if content_type else None
    _container_client().upload_blob(name=key, data=data, overwrite=True, content_settings=cs)
    # Fires only when a file is actually stored in Azurite/Azure blob storage
    # (every blob write — BDX, mapper, export, contract — funnels through here).
    _log.info("[file-storage] UPLOAD -> blob storage | container=%s key=%s size=%d bytes",
              _container_name(), key, len(data) if data else 0)
    return key


def get_bytes(key: str) -> bytes:
    data = _container_client().download_blob(key).readall()
    _log.info("[file-storage] DOWNLOAD <- blob storage | key=%s size=%d bytes",
              key, len(data) if data else 0)
    return data


def delete_blob(key: str) -> None:
    try:
        _container_client().delete_blob(key)
    except Exception as e:                          # noqa: BLE001 — best-effort cleanup
        _log.warning("delete_blob(%s) failed: %s", key, e)


# ── High-level helpers used by endpoints ────────────────────────────────────

def store_or_keep(category: str, tenant_id, filename: str, data: bytes,
                  content_type: str | None = None):
    """Persist `data` and return the pair to assign to the (*_ref, legacy_blob)
    columns.

    Azure mode -> (blob_key, None); DB mode -> (None, data). Returns (None, None)
    for empty input. Blocking (Azure client is sync) — wrap in run_in_threadpool
    when calling from an async endpoint.
    """
    if not data:
        return None, None
    if is_azure():
        key = build_key(category, tenant_id, filename)
        put_bytes(key, data, content_type)
        return key, None
    return None, data


def resolve_bytes(ref: str | None, legacy: bytes | None) -> bytes | None:
    """Return the file bytes from wherever they live — blob ref preferred, then
    the legacy DB column — or None if neither is present. Blocking; fine in sync
    endpoints, wrap in run_in_threadpool from async code."""
    if ref:
        return get_bytes(ref)
    if legacy is not None:
        return bytes(legacy)
    return None
