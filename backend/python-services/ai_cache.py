"""
ai_cache.py
───────────
Memoization for model calls whose answer does NOT depend on the uploaded file —
see db.AiResponseCache for which calls those are and why.

Contract with every caller:
  • A hit returns EXACTLY the payload the model returned on the miss, so the
    caller's downstream behaviour is identical either way. Nothing here
    interprets, trims or re-shapes an answer.
  • Every function is FAIL-OPEN. A missing table, a dead connection, an
    unserializable payload — all are swallowed and reported as a miss, so the
    caller makes the model call and the pipeline behaves as it did before this
    module existed. Caching must never be able to break an upload.
  • The key must cover EVERY input that can change the answer. Use make_key()
    and pass all of them; a forgotten input is how a stale answer gets served.

Disable entirely with KAVACHIO_AI_CACHE=0, or per kind with
KAVACHIO_AI_CACHE_<KIND>=0 (e.g. KAVACHIO_AI_CACHE_GENERIC_BIND=0).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime


def _enabled(kind: str) -> bool:
    if os.getenv("KAVACHIO_AI_CACHE", "1") == "0":
        return False
    return os.getenv(f"KAVACHIO_AI_CACHE_{kind.upper()}", "1") != "0"


def make_key(*parts) -> str:
    """SHA-256 over the canonical JSON of every input that can change the answer.

    sort_keys makes dict ordering irrelevant (template field dicts are built in
    varying order); default=str keeps dates/Decimals from raising. The goal is a
    STABLE fingerprint, not a reversible encoding.
    """
    blob = json.dumps(parts, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(kind: str, key: str):
    """Cached payload for (kind, key), or None on a miss / any failure."""
    if not _enabled(kind) or not key:
        return None
    try:
        from db import SessionLocal, AiResponseCache
        with SessionLocal() as s:
            row = (s.query(AiResponseCache)
                   .filter(AiResponseCache.kind == kind,
                           AiResponseCache.cache_key == key)
                   .first())
            if row is None:
                try:
                    import pipeline_log as plog
                    plog.log("CACHE", "MISS", f"{kind} {key[:12]}",
                             "no stored answer for this exact input — model call required")
                except Exception:
                    pass
                return None
            payload = row.payload
            # Usage counters are bookkeeping, not the answer — a write failure
            # here must never turn a good hit into a miss.
            try:
                row.hits = (row.hits or 0) + 1
                row.last_used_at = datetime.utcnow()
                s.commit()
            except Exception:
                s.rollback()
            print(f"[ai-cache] HIT {kind} {key[:12]}… — model call skipped")
            try:
                import pipeline_log as plog
                plog.log("CACHE", "HIT", f"{kind} {key[:12]}",
                         "answer reused — this model call did NOT happen")
            except Exception:
                pass
            return payload
    except Exception as exc:  # noqa: BLE001 — fail-open, see module docstring
        print(f"[ai-cache] lookup skipped ({kind}): {exc}")
        return None


def put(kind: str, key: str, payload, tenant_id=None) -> None:
    """Store a payload. Silent no-op on any failure."""
    if not _enabled(kind) or not key or payload is None:
        return
    try:
        from db import SessionLocal, AiResponseCache
        with SessionLocal() as s:
            row = (s.query(AiResponseCache)
                   .filter(AiResponseCache.kind == kind,
                           AiResponseCache.cache_key == key)
                   .first())
            if row is None:
                s.add(AiResponseCache(tenant_id=tenant_id, kind=kind,
                                      cache_key=key, payload=payload, hits=0))
            else:
                row.payload = payload
                row.last_used_at = datetime.utcnow()
            s.commit()
        print(f"[ai-cache] STORED {kind} {key[:12]}…")
    except Exception as exc:  # noqa: BLE001 — fail-open, see module docstring
        print(f"[ai-cache] store skipped ({kind}): {exc}")
