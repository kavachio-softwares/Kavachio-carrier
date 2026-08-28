"""Heartbeat streaming for long-running endpoints.

The Azure Container Apps ingress kills a connection idle for ~4 minutes. Some
endpoints (contract extraction, bordereau processing) legitimately run longer.
This helper runs the heavy work as a background task while streaming a single
whitespace byte every HEARTBEAT_SECS to reset the idle timer; the real JSON
payload is the final chunk. Leading whitespace is ignored by JSON parsers, so
the accumulated body is still one valid JSON document.

Trade-off: once streaming starts the HTTP status is locked at 200, so a failure
that happens after the first byte is reported IN the body as
{success:false, error:true, status_code, detail}. The frontend axios
interceptor converts that back into a thrown error with the same {detail} shape
as an HTTPException, so callers' existing catch paths keep working.
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

HEARTBEAT_SECS = 20


def heartbeat_stream_response(
    work: Awaitable,
    *,
    on_done: Optional[Callable[["asyncio.Task"], None]] = None,
    media_type: str = "application/json",
) -> StreamingResponse:
    """Run `work` (an awaitable producing a JSON-serialisable result) under a
    heartbeat stream. `on_done(task)` runs when the task settles (e.g. temp-file
    cleanup) — errors in it are swallowed. Anything the caller wants returned as
    a real 4xx must be validated BEFORE calling this (once the stream starts the
    status is 200)."""
    task = asyncio.ensure_future(work)

    def _on_done(t: "asyncio.Task") -> None:
        # Mark any exception as retrieved (the client may disconnect before the
        # stream reads task.result()) so it isn't logged as "never retrieved".
        if not t.cancelled():
            t.exception()
        if on_done is not None:
            try:
                on_done(t)
            except Exception:  # noqa: BLE001 — cleanup must never break the response
                pass

    task.add_done_callback(_on_done)

    async def _stream():
        yield b" "  # flush headers + first byte immediately
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_SECS)
            if done:
                break
            yield b" "
        try:
            result = task.result()
        except Exception as e:  # noqa: BLE001
            status_code = e.status_code if isinstance(e, HTTPException) else 500
            detail = e.detail if isinstance(e, HTTPException) else str(e)
            result = {
                "success": False, "error": True,
                "status_code": status_code, "detail": detail,
            }
        yield json.dumps(jsonable_encoder(result)).encode()

    return StreamingResponse(
        _stream(),
        media_type=media_type,
        # Ask intermediaries to pass chunks through unbuffered.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
