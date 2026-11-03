"""Every route is served by the handler it was written for.

THE BREAK. A FastAPI decorator binds to whatever `def` comes next. Helpers were
added between `@router.post("/direct/run")` and `async def direct_run(`, so the
first helper became the route: Generate BDX on Process Bordereau posted to
`_run_contract_for_render(chosen, pipeline_id, legacy_fallback)`, every click
came back 422 for three query parameters no screen sends, and `direct_run`
itself — the setup lookup, the render, the rules — was never reached.

Nothing else noticed. The helpers' own tests call them directly, and the
broker's lane calls `direct_run` as a plain function, so both kept passing
while the carrier's screen could not run a single bordereau.

    python -m pytest test_route_bindings.py
"""
from __future__ import annotations

from fastapi.routing import APIRoute

import main
import direct_routes as dr


def _routes() -> list[APIRoute]:
    return [r for r in main.app.routes if isinstance(r, APIRoute)]


def _run_route() -> list[APIRoute]:
    return [r for r in _routes() if r.path == "/direct/run" and "POST" in r.methods]


def test_generate_bdx_posts_to_the_run_handler():
    assert [r.endpoint for r in _run_route()] == [dr.direct_run]


def test_the_run_asks_only_for_what_the_screen_sends():
    run, = _run_route()
    sent = {p.name for p in run.dependant.body_params}
    assert {"mga", "carrier_party_id", "program_id", "file",
            "broker_party_id", "contract_id", "check_only"} <= sent
    # The screen posts a form and nothing else; a required query parameter is
    # a request it can never satisfy — exactly how the break above looked.
    assert [p.name for p in run.dependant.query_params if p.required] == []


def test_no_route_is_served_by_a_private_helper():
    stray = sorted(f"{'/'.join(sorted(r.methods))} {r.path} -> {r.endpoint.__name__}"
                   for r in _routes() if r.endpoint.__name__.startswith("_"))
    assert stray == []
