"""Feature 10 — HTTP surface for "How Files Arrive" and "Files Received".

Mounted by main.py under /intake. Every path here is new, so nothing that
already exists changes behaviour.

The screens these back are Configure screens, not part of the monthly run: a
route is set up once when a broker is onboarded and then rarely touched. That
is why this is plain CRUD over `intake_route` plus a read of `file_arrival` —
the interesting work happens in intake_service and sftp_poller.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func

import intake_events
import intake_review as review
import intake_service as svc
from app_routes import (
    _actor, _iso_utc, _log, _tenant_name, assert_tenant_owns, resolve_tenant_id,
)
from auth_deps import Principal, current_principal, require_role
from db import AppUser, Party, Program, ProgramBroker, SessionLocal
from intake_auth import mask, mint_key
from intake_models import FileArrival, IntakeCredential, IntakeRoute

router = APIRouter(prefix="/intake", tags=["intake"])

# The five ways in. Fixed on purpose — "you cannot invent a sixth". Adding a
# route gives ONE BROKER their own address on one of these, it does not create
# a new kind of door.
CHANNELS = ("upload", "email", "sftp", "api", "cloud_folder")

# Ways in we go and FETCH from. SFTP looks in a folder (10.1); email reads a
# mailbox (10.3). An API route is live but pushed to, so there is nothing to
# fetch — which is why this is a narrower list than CREATABLE_CHANNELS below.
COLLECTING_CHANNELS = ("sftp", "email")

# Ways in that are wired end to end and can be created from the screen. SFTP
# pulls from a folder (10.1); API is pushed to (10.2); email reads a mailbox
# (10.3).
CREATABLE_CHANNELS = ("sftp", "api", "email")


# ── request bodies ──────────────────────────────────────────────────────────

class RouteCreate(BaseModel):
    channel: str
    broker_party_id: int
    display_name: Optional[str] = None
    file_style: str = "whole_book"
    note: Optional[str] = None
    # 10.2 — pin the route to one programme. Leave NULL for the old
    # broker-wide behaviour; set it and an API key on this route needs no
    # programme on the call at all. Recommended: one route (and one key) per
    # (programme, broker) pair.
    program_id: Optional[int] = None
    # 10.3 — REQUIRED for an email route: the address this broker sends FROM.
    # Every broker emails the same inbox, so the mailbox cannot tell one route
    # from another; who the mail comes from is what does. See
    # email_intake_service.build_email_address.
    sender_email: Optional[str] = None


class KeyCreate(BaseModel):
    label: Optional[str] = None
    ip_allowlist: Optional[list[str]] = None


class RoutePatch(BaseModel):
    # Both of the settings modal's live controls, and nothing else — the rest of
    # that panel is fixed behaviour, not configuration.
    file_style: Optional[str] = None
    is_enabled: Optional[bool] = None
    display_name: Optional[str] = None
    note: Optional[str] = None


# ── serialisers ─────────────────────────────────────────────────────────────

def _mail_cfg():
    """The intake mailbox, resolved at call time (the .env is loaded after
    import). Imported lazily so this module still loads if email intake is not
    configured at all."""
    import email_intake_service as mailsvc
    return mailsvc.mailbox_config()


def _mail_ready() -> bool:
    try:
        import email_intake_service as mailsvc
        return mailsvc.is_configured()
    except Exception:
        return False


def _collector_status() -> dict:
    """How each pulled channel is noticing files right now.

    "The moment it lands" is only true while the folder watcher or the IDLE
    connection is actually up, so the screen reads this rather than printing a
    promise. Per process — every replica runs its own collectors.
    """
    import email_poller
    import sftp_poller
    return {"sftp": sftp_poller.status(), "email": email_poller.status()}


def _send_to(r: IntakeRoute) -> Optional[str]:
    """The address to hand THIS broker, for an email route.

    Plus-addressing, so the address a broker is given identifies them the way an
    SFTP folder does — bordereaux+bridge-brokers@… can only be arrived at by
    being told it. Falls back to the plain mailbox when the server does not
    support tags; the From: match still resolves the route either way.
    """
    if r.channel != "email":
        return None
    mailbox = _mail_cfg().user
    if not mailbox or "@" not in mailbox:
        return None
    import email_intake_service as mailsvc
    local, _, domain = mailbox.partition("@")
    return f"{local}+{mailsvc.route_label(r)}@{domain}"


def _route_dict(r: IntakeRoute, broker_name: Optional[str],
                files_this_month: int = 0,
                program_name: Optional[str] = None) -> dict:
    return {
        "route_id": r.id,
        "channel": r.channel,
        "address": r.address,
        "display_address": svc.display_address(r),
        # Email is the one channel where the address a broker SENDS FROM and the
        # address they SEND TO are different things, so the screen needs both.
        "send_to": _send_to(r),
        "display_name": r.display_name,
        "broker_party_id": r.broker_party_id,
        "broker_name": broker_name,
        "program_id": getattr(r, "program_id", None),
        "program_name": program_name,
        "is_enabled": bool(r.is_enabled),
        "file_style": r.file_style,
        "fallback_rank": r.fallback_rank,
        "note": r.note,
        "files_this_month": files_this_month,
        "collecting": r.channel in COLLECTING_CHANNELS,
        "created_at": _iso_utc(r.created_at),
        "disabled_at": _iso_utc(r.disabled_at),
    }


def _broker_names(session, tenant_id: int) -> dict[int, str]:
    rows = (session.query(Party.id, Party.legal_name)
            .filter(Party.tenant_id == tenant_id).all())
    return {pid: name for pid, name in rows}


# ── routes ──────────────────────────────────────────────────────────────────

@router.get("/routes")
def list_routes(mga: Optional[str] = None,
                principal: Principal = Depends(current_principal)):
    """Everything the "How Files Arrive" screen needs, in one call: the routes,
    this month's counts, the brokers that can be given one, and the tile totals.

    One call rather than four because the screen is useless with any of them
    missing — a partial render would show "0 files" next to a live route.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        rows = (s.query(IntakeRoute)
                .filter(IntakeRoute.tenant_id == tid)
                .order_by(IntakeRoute.channel, IntakeRoute.id).all())
        names = _broker_names(s, tid)
        counts = svc.month_counts(s, tid)
        _prog_names = {p.id: p.name for p in
                       s.query(Program).filter(Program.tenant_id == tid).all()}
        routes = [_route_dict(r, names.get(r.broker_party_id), counts.get(r.id, 0),
                              _prog_names.get(getattr(r, "program_id", None)))
                  for r in rows]

        # Brokers that may be given a way in: those actually on one of this
        # carrier's programmes. A broker with no programme cannot receive a
        # route, because there would be nothing for their files to belong to.
        broker_ids = [bid for (bid,) in
                      s.query(ProgramBroker.broker_party_id)
                      .filter(ProgramBroker.tenant_id == tid,
                              ProgramBroker.status == "active")
                      .distinct().all()]
        brokers = [{"party_id": bid, "legal_name": names.get(bid, f"Party {bid}")}
                   for bid in broker_ids if bid in names]
        brokers.sort(key=lambda b: b["legal_name"].lower())

        # Which programmes each broker is on. An API key is best scoped to ONE
        # (programme, broker) pair — then the sender supplies nothing at all —
        # so the Add dialog has to be able to offer the choice.
        prog_names = {p.id: p.name for p in
                      s.query(Program).filter(Program.tenant_id == tid).all()}
        broker_programmes: dict[str, list] = {}
        for pb in (s.query(ProgramBroker)
                   .filter(ProgramBroker.tenant_id == tid,
                           ProgramBroker.status == "active").all()):
            broker_programmes.setdefault(str(pb.broker_party_id), []).append(
                {"program_id": pb.program_id,
                 "name": prog_names.get(pb.program_id, f"Programme {pb.program_id}")})
        for v in broker_programmes.values():
            v.sort(key=lambda x: (x["name"] or "").lower())

        # The addresses we already know for each broker, so the email dialog can
        # offer them instead of asking somebody to retype one.
        #
        # These are PORTAL LOGINS, not sending addresses — the person who signs
        # in is often not the mailbox their export job sends as. So they are
        # offered as a suggestion the screen fills in and the user can overwrite,
        # never as a value taken on trust. Active first: an unaccepted invite is
        # not evidence of a working mailbox.
        broker_emails: dict[str, list] = {}
        if broker_ids:
            for u in (s.query(AppUser)
                      .filter(AppUser.broker_party_id.in_(list(broker_ids)))
                      .all()):
                if not u.email:
                    continue
                broker_emails.setdefault(str(u.broker_party_id), []).append(
                    {"email": u.email, "name": u.full_name,
                     "status": u.status or "active"})
        for v in broker_emails.values():
            v.sort(key=lambda x: (x["status"] != "active", x["email"].lower()))

        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        arrivals_q = s.query(FileArrival).filter(
            FileArrival.tenant_id == tid, FileArrival.received_at >= month_start)
        files_this_month = arrivals_q.count()
        turned_away = arrivals_q.filter(FileArrival.outcome == "turned_away").count()
        held = arrivals_q.filter(FileArrival.outcome == "held").count()

        per_channel: dict[str, int] = {}
        for r in rows:
            per_channel[r.channel] = per_channel.get(r.channel, 0) + counts.get(r.id, 0)
        most_used = max(per_channel, key=per_channel.get) if per_channel else None

        return {
            "routes": routes,
            "brokers": brokers,
            "broker_programmes": broker_programmes,
            "broker_emails": broker_emails,
            "channels": list(CHANNELS),
            "collecting": list(COLLECTING_CHANNELS),
            "creatable": list(CREATABLE_CHANNELS),
            "sftp_host": svc.sftp_host(),
            # 10.3 — the inbox brokers send TO. Config, not data, exactly like
            # sftp_host: a route stores who a broker sends FROM, so moving the
            # intake mailbox must not strand every stored address.
            "email_mailbox": _mail_cfg().user or None,
            "email_ready": _mail_ready(),
            "collector": _collector_status(),
            "tiles": {
                "ways_on": sum(1 for r in rows if r.is_enabled),
                "ways_total": len(CHANNELS),
                "files_this_month": files_this_month,
                "most_used_channel": most_used,
                "most_used_files": per_channel.get(most_used, 0) if most_used else 0,
                "turned_away": turned_away,
                "held": held,
            },
        }


@router.post("/routes")
def create_route(body: RouteCreate, mga: Optional[str] = None,
                 principal: Principal = Depends(require_role("carrier_admin"))):
    """Give one broker their own address on one of the five ways in.

    The address is generated, never typed. It is the deliverable of this screen
    and it has to match the folder the collector looks in exactly — one typo in
    a hand-entered path is a broker whose files are never picked up, with no
    error anywhere to explain why.
    """
    if body.channel not in CHANNELS:
        raise HTTPException(400, f"channel must be one of {', '.join(CHANNELS)}")
    if body.channel not in CREATABLE_CHANNELS:
        raise HTTPException(400, f"'{body.channel}' is not built yet — "
                                 f"you can create {' or '.join(CREATABLE_CHANNELS)}")
    if body.file_style not in ("whole_book", "changes_only"):
        raise HTTPException(400, "file_style must be whole_book or changes_only")

    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        broker = s.get(Party, body.broker_party_id)
        if broker is None:
            raise HTTPException(404, "broker not found")
        assert_tenant_owns(principal, broker.tenant_id)

        carrier_name = _tenant_name(s, tid) or "carrier"
        if body.program_id is not None:
            prog = s.get(Program, body.program_id)
            if prog is None or prog.tenant_id != tid:
                raise HTTPException(404, "programme not found on this account")
            if not (s.query(ProgramBroker)
                    .filter(ProgramBroker.program_id == body.program_id,
                            ProgramBroker.broker_party_id == broker.id,
                            ProgramBroker.status == "active").first()):
                raise HTTPException(400, f"{broker.legal_name} is not on that "
                                         "programme")
        # Each channel identifies a sender differently, so each derives its
        # address differently. An API caller has no folder, so the "address" is
        # the endpoint — the same for everyone, and the KEY is what differs. An
        # email route stores who the broker sends FROM, because every broker
        # emails the same inbox.
        if body.channel == "api":
            address = "POST /v1/bordereaux"
        elif body.channel == "email":
            import email_intake_service as mailsvc
            try:
                address = mailsvc.build_email_address(body.sender_email or "")
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        else:
            address = svc.build_sftp_address(carrier_name, broker.legal_name)

        existing = None
        if body.channel != "api":
            existing = (s.query(IntakeRoute)
                        .filter(IntakeRoute.tenant_id == tid,
                                IntakeRoute.channel == body.channel,
                                IntakeRoute.address == address).first())
        if existing is not None:
            # UNIQUE (tenant_id, channel, address) would raise anyway; saying
            # which broker already holds it is more use than a 500.
            raise HTTPException(409, f"{broker.legal_name} already has a "
                                     f"{body.channel} way in at {address}")

        route = IntakeRoute(
            tenant_id=tid,
            broker_party_id=broker.id,
            channel=body.channel,
            address=address,
            display_name=body.display_name or broker.legal_name,
            is_enabled=True,
            file_style=body.file_style,
            note=body.note,
            program_id=body.program_id,
            created_by_user_id=getattr(principal, "user_id", None),
        )
        s.add(route)
        s.flush()

        # Create the folders now, not on first file. A broker given an address
        # will test it immediately, and an SFTP put into a folder that does not
        # exist fails with a permission error that looks like a credential
        # problem — the hardest kind of support call to answer.
        created_dir = None
        if route.channel == "sftp":
            try:
                created_dir = str(svc.ensure_route_dirs(route))
            except OSError as exc:
                raise HTTPException(500, f"could not create the folder: {exc}")

        s.commit()
        s.refresh(route)
        _log(_tenant_name(s, tid) or "", _actor(principal), "intake_route_created",
             target=str(route.id),
             details={"channel": route.channel, "address": route.address,
                      "broker_party_id": route.broker_party_id})
        out = _route_dict(route, broker.legal_name, 0)
        out["folder"] = created_dir
        return out


@router.patch("/routes/{route_id}")
def patch_route(route_id: int, body: RoutePatch, mga: Optional[str] = None,
                principal: Principal = Depends(require_role("carrier_admin"))):
    """The settings panel. Only two things are actually configurable."""
    if body.file_style is not None and body.file_style not in ("whole_book", "changes_only"):
        raise HTTPException(400, "file_style must be whole_book or changes_only")

    with SessionLocal() as s:
        route = s.get(IntakeRoute, route_id)
        if route is None:
            raise HTTPException(404, "route not found")
        assert_tenant_owns(principal, route.tenant_id)

        for field, value in body.model_dump(exclude_unset=True).items():
            if field == "is_enabled":
                # Switching off is not deleting. Files that already came in this
                # way keep their history; only new ones are refused.
                route.disabled_at = None if value else datetime.now(timezone.utc)
                route.disabled_by_user_id = (
                    None if value else getattr(principal, "user_id", None))
            setattr(route, field, value)

        s.commit()
        s.refresh(route)
        names = _broker_names(s, route.tenant_id)
        _log(_tenant_name(s, route.tenant_id) or "", _actor(principal),
             "intake_route_updated", target=str(route.id),
             details=body.model_dump(exclude_unset=True))
        return _route_dict(route, names.get(route.broker_party_id),
                           svc.month_counts(s, route.tenant_id).get(route.id, 0))


@router.get("/arrivals")
def list_arrivals(mga: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
                  principal: Principal = Depends(current_principal)):
    """The "Files Received" screen: everything that landed, however it landed.

    Accepted and refused come back in one list with the outcome on each row,
    rather than as two queries. The screen splits them, but they are one
    timeline and paging them separately would let a refusal fall off the end
    while the accepted file above it stayed visible.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        rows = (s.query(FileArrival)
                .filter(FileArrival.tenant_id == tid)
                .order_by(FileArrival.received_at.desc())
                .limit(limit).all())
        names = _broker_names(s, tid)
        routes = {r.id: r for r in s.query(IntakeRoute)
                  .filter(IntakeRoute.tenant_id == tid).all()}
        # A route pinned to a programme (10.2) is what tells an arrival which
        # programme it belongs to. A broker-wide route knows WHO but not WHICH,
        # and the screen shows that honestly rather than guessing.
        prog_names = {p.id: p.name for p in
                      s.query(Program).filter(Program.tenant_id == tid).all()}

        out = []
        for a in rows:
            route = routes.get(a.route_id)
            out.append({
                "arrival_id": a.id,
                "filename": a.filename,
                "channel": route.channel if route else None,
                "route_id": a.route_id,
                "route_address": route.address if route else None,
                "broker_party_id": a.matched_broker_party_id,
                "broker_name": names.get(a.matched_broker_party_id),
                "claimed_sender": a.claimed_sender,
                "file_size_bytes": a.file_size_bytes,
                # Rows, not kilobytes — what a bordereau is actually measured
                # in. NULL on rows that arrived before the column existed.
                "row_count": a.row_count,
                "file_hash_sha256": a.file_hash_sha256,
                # The programme the route is pinned to, so the screen does not
                # have to join arrivals to routes itself.
                "program_id": getattr(route, "program_id", None) if route else None,
                "program_name": prog_names.get(
                    getattr(route, "program_id", None)) if route else None,
                "received_at": _iso_utc(a.received_at),
                "outcome": a.outcome,
                "turned_away_reason": a.turned_away_reason,
                # SFTP has no reply path — a file dropped in a folder nobody owns
                # has nobody to tell. The screen says so rather than showing blank.
                "sender_notified_at": _iso_utc(a.sender_notified_at),
                "sender_notified_via": a.sender_notified_via,
                "bdx_upload_id": a.bdx_upload_id,
                "public_ref": a.public_ref,
                # ── 12.3 — what a person decided, and whether the file is
                # still there to look at. The screen needs both: a resolved row
                # must stop asking to be worked, and a download button that
                # cannot work should not be offered.
                "resolution": a.resolution,
                "resolved_at": _iso_utc(a.resolved_at),
                "resolved_by_user_id": a.resolved_by_user_id,
                "resolution_note": a.resolution_note,
                "bytes_purged_at": _iso_utc(a.bytes_purged_at),
                "can_download": bool(
                    a.blob_ref and a.bytes_purged_at is None
                    and not review.is_infected(a)),
                "is_infected": review.is_infected(a),
            })
        counts = {
            "total": len(out),
            "accepted": sum(1 for a in out if a["outcome"] == "accepted"),
            "held": sum(1 for a in out if a["outcome"] == "held"),
            "turned_away": sum(1 for a in out if a["outcome"] == "turned_away"),
            # The only number anybody has to act on: held, and nobody has
            # looked at it yet. This is what the queue is.
            "waiting": sum(1 for a in out
                           if a["outcome"] == "held" and not a["resolution"]),
        }
        return {"rows": out, "counts": counts}


# ── the review queue (feature 12.3) ─────────────────────────────────────────
# A refused file is kept so somebody can look at it. Until these three
# endpoints existed there was nothing to look WITH: the screen's buttons were
# built disabled because a decision had nowhere to be recorded.


class ReviewDecision(BaseModel):
    note: Optional[str] = None


def _arrival_for_review(s, arrival_id: int, principal: Principal) -> FileArrival:
    arrival = s.get(FileArrival, arrival_id)
    if arrival is None:
        raise HTTPException(404, "no such arrival")
    assert_tenant_owns(principal, arrival.tenant_id)
    return arrival


@router.post("/arrivals/{arrival_id}/release")
def release_arrival(arrival_id: int, body: ReviewDecision = ReviewDecision(),
                    principal: Principal = Depends(require_role("carrier_admin"))):
    """"This is fine, load it." Held files only — see intake_review.release."""
    with SessionLocal() as s:
        arrival = _arrival_for_review(s, arrival_id, principal)
        try:
            review.release(s, arrival, user_id=principal.user_id, note=body.note)
        except review.ReviewError as exc:
            raise HTTPException(400, str(exc))
        s.commit()
        return {"arrival_id": arrival.id, "outcome": arrival.outcome,
                "resolution": arrival.resolution,
                "resolved_at": _iso_utc(arrival.resolved_at)}


@router.post("/arrivals/{arrival_id}/discard")
def discard_arrival(arrival_id: int, body: ReviewDecision = ReviewDecision(),
                    principal: Principal = Depends(require_role("carrier_admin"))):
    """"Ignore this." The row stays; only the decision is added."""
    with SessionLocal() as s:
        arrival = _arrival_for_review(s, arrival_id, principal)
        try:
            review.discard(s, arrival, user_id=principal.user_id, note=body.note)
        except review.ReviewError as exc:
            raise HTTPException(400, str(exc))
        s.commit()
        return {"arrival_id": arrival.id, "outcome": arrival.outcome,
                "resolution": arrival.resolution,
                "resolved_at": _iso_utc(arrival.resolved_at)}


@router.get("/arrivals/{arrival_id}/download")
def download_arrival(arrival_id: int, request: Request,
                     principal: Principal = Depends(require_role("carrier_admin"))):
    """The stored copy, so a reviewer can open what they are judging.

    Never for a file that failed the security scan, and every read is logged —
    these are files that failed inspection, and one day somebody will ask who
    looked at one.
    """
    with SessionLocal() as s:
        arrival = _arrival_for_review(s, arrival_id, principal)
        try:
            data = review.fetch_bytes(
                arrival, user_id=principal.user_id,
                ip=request.client.host if request.client else None)
        except review.ReviewError as exc:
            raise HTTPException(400, str(exc))
        safe_name = (arrival.filename or "file").replace('"', "")
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}"'})


@router.post("/retention/run")
def run_retention(principal: Principal = Depends(require_role("carrier_admin"))):
    """Run the retention sweep now rather than waiting for the nightly task.

    Exists for the same reason /routes/{id}/poll does: a rule that only ever
    runs at 3am cannot be demonstrated, and "did it work?" should have an answer
    on screen rather than in a log file next week.
    """
    return review.run_retention()


@router.get("/events")
async def arrival_events(request: Request, mga: Optional[str] = None,
                         principal: Principal = Depends(current_principal)):
    """A live feed for the Files screen: one line whenever this carrier's
    files change — a file landed by any way in, or somebody decided one.

    Replaces the screen asking for the whole list every 60 seconds. Newline-
    delimited JSON (`ready`, then `arrivals` / `ping`), read with fetch so the
    bearer token travels in a header like every other call. The lines carry no
    data at all — the screen re-reads /intake/arrivals, which is where the
    tenant scoping lives. See intake_events.
    """
    def _tenant() -> int:
        with SessionLocal() as s:
            return resolve_tenant_id(s, principal, mga)

    tid = await run_in_threadpool(_tenant)
    return StreamingResponse(
        intake_events.stream(tid, request),
        media_type="application/x-ndjson",
        # A proxy that buffers the response would hold every line back until
        # the stream ends, which is the whole point defeated.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/routes/{route_id}/poll")
def poll_route(route_id: int, principal: Principal = Depends(require_role("carrier_admin"))):
    """Collect this route's folder or mailbox right now.

    The screen no longer needs this — files are collected the moment they land
    (sftp_watch, IMAP IDLE) — so it has no button any more. It stays for support
    and scripts: the one-call answer to "is this folder working?", with a
    summary of exactly what was found.
    """
    with SessionLocal() as s:
        route = s.get(IntakeRoute, route_id)
        if route is None:
            raise HTTPException(404, "route not found")
        assert_tenant_owns(principal, route.tenant_id)
        if route.channel not in COLLECTING_CHANNELS:
            raise HTTPException(400, f"nothing collects from '{route.channel}' yet")
        # Each collecting channel fetches from somewhere different — a folder
        # for SFTP, a mailbox for email — so the collector is chosen by channel
        # rather than there being one that understands both.
        if route.channel == "email":
            from email_poller import collect_route
        else:
            from sftp_poller import collect_route
        result = collect_route(s, route)
        s.commit()
        return result


# ── API keys (feature 10.2) ─────────────────────────────────────────────────
# An API route has no folder to identify a sender by, so the key does that job.
# Everything else about the route — broker, programme, on/off — is unchanged.

MAX_LIVE_KEYS = 2      # an overlap for rotation, without letting keys pile up


@router.post("/routes/{route_id}/keys", status_code=201)
def create_key(route_id: int, body: KeyCreate,
               principal: Principal = Depends(require_role("carrier_admin"))):
    """Mint a key. The plaintext is returned exactly ONCE and never stored —
    the screen has to say so plainly before it disappears."""
    with SessionLocal() as s:
        route = s.get(IntakeRoute, route_id)
        if route is None:
            raise HTTPException(404, "route not found")
        assert_tenant_owns(principal, route.tenant_id)
        if route.channel != "api":
            raise HTTPException(400, "only an API way in has keys")

        live = (s.query(IntakeCredential)
                .filter(IntakeCredential.route_id == route_id,
                        IntakeCredential.revoked_at.is_(None)).count())
        if live >= MAX_LIVE_KEYS:
            raise HTTPException(409, f"this way in already has {live} live keys "
                                     "— revoke one before creating another")

        full, prefix, digest = mint_key()
        cred = IntakeCredential(
            route_id=route.id, tenant_id=route.tenant_id, key_prefix=prefix,
            key_hash=digest, last4=full[-4:], label=body.label,
            ip_allowlist=body.ip_allowlist,
            created_by_user_id=getattr(principal, "user_id", None))
        s.add(cred)
        s.commit()
        s.refresh(cred)
        _log(_tenant_name(s, route.tenant_id) or "", _actor(principal),
             "intake_key_created", target=str(cred.id),
             details={"route_id": route.id, "label": cred.label})
        return {
            "credential_id": cred.id,
            "label": cred.label,
            "api_key": full,
            "warning": "Copy this now. It is not stored and cannot be shown again.",
        }


@router.get("/routes/{route_id}/keys")
def list_keys(route_id: int, principal: Principal = Depends(current_principal)):
    with SessionLocal() as s:
        route = s.get(IntakeRoute, route_id)
        if route is None:
            raise HTTPException(404, "route not found")
        assert_tenant_owns(principal, route.tenant_id)
        rows = (s.query(IntakeCredential)
                .filter(IntakeCredential.route_id == route_id)
                .order_by(IntakeCredential.id.desc()).all())
        return [{
            "credential_id": c.id,
            "label": c.label,
            "key": mask(c.key_prefix, c.last4),      # never the key itself
            "created_at": _iso_utc(c.created_at),
            "last_used_at": _iso_utc(c.last_used_at),
            "revoked_at": _iso_utc(c.revoked_at),
            "is_live": c.revoked_at is None,
        } for c in rows]


@router.delete("/keys/{credential_id}")
def revoke_key(credential_id: int,
               principal: Principal = Depends(require_role("carrier_admin"))):
    """Revoke, never delete. Every file that arrived on this key still points at
    it, and deleting the row would stop the history answering "who sent this?"."""
    with SessionLocal() as s:
        cred = s.get(IntakeCredential, credential_id)
        if cred is None:
            raise HTTPException(404, "key not found")
        assert_tenant_owns(principal, cred.tenant_id)
        if cred.revoked_at is None:
            cred.revoked_at = datetime.now(timezone.utc)
            s.commit()
            _log(_tenant_name(s, cred.tenant_id) or "", _actor(principal),
                 "intake_key_revoked", target=str(cred.id))
        return {"credential_id": cred.id, "revoked_at": _iso_utc(cred.revoked_at)}
