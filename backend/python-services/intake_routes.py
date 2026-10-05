"""Feature 10 — HTTP surface for "How Files Arrive" and "Files Received".

Mounted by main.py under /intake. Every path here is new, so nothing that
already exists changes behaviour.

The screens these back are Configure screens, not part of the monthly run: a
route is set up once when a broker is onboarded and then rarely touched. That
is why this is plain CRUD over `intake_route` plus a read of `file_arrival` —
the interesting work happens in intake_service and sftp_poller.
"""
from __future__ import annotations

import logging
import os
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
import sftp_pull
from app_routes import (
    _actor, _iso_utc, _log, _tenant_name, assert_tenant_owns, resolve_tenant_id,
)
from auth_deps import Principal, current_principal, require_role
from db import AppUser, Party, Program, ProgramBroker, SessionLocal
from intake_auth import mask, mint_key
from intake_models import FileArrival, IntakeCredential, IntakeRoute

router = APIRouter(prefix="/intake", tags=["intake"])
log = logging.getLogger(__name__)

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

class SftpTestBody(BaseModel):
    """An EXTERNAL SFTP server Kavachio logs in to and collects from (see
    sftp_pull). The password / key comes in here and is never sent back out.
    Everything has a default so a half-filled form gets a plain-words answer
    from the test rather than a validation error."""
    host: str = ""
    port: Optional[int] = 22
    username: str = ""
    auth: str = "password"                 # password | key
    password: Optional[str] = None
    private_key: Optional[str] = None
    passphrase: Optional[str] = None
    remote_dir: str = ""                   # "" = the folder the login starts in
    processed_dir: Optional[str] = None    # default "<remote_dir>/processed"
    after: str = "move"                    # move | delete


class SftpSettings(SftpTestBody):
    interval_minutes: int = 15             # 5 | 15 | 60
    # The fingerprint /intake/sftp/test showed. It is PINNED: the route trusts
    # that server key and no other, so it must still be the one presented now.
    fingerprint: Optional[str] = None


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
    # SFTP — the server Kavachio collects FROM (sftp_pull). Without it an SFTP
    # route is the old kind, a folder on Kavachio's own server (sftp_poller):
    # still supported, no longer offered on the screen.
    sftp: Optional[SftpSettings] = None


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
    return {"sftp": sftp_poller.status(), "email": email_poller.status(),
            "sftp_pull": sftp_pull.status()}


def _send_to(r: IntakeRoute) -> Optional[str]:
    """The address to hand THIS broker, for an email route: Kavachio's intake
    mailbox, the same for every broker. Who sent it (From:) and which carrier
    is copied (Cc) say whose file it is — see email_intake_service."""
    if r.channel != "email":
        return None
    mailbox = _mail_cfg().user
    return mailbox if mailbox and "@" in mailbox else None


def _carrier_cc(s, tenant_id: int) -> Optional[str]:
    """The carrier address a broker must copy on every bordereau email."""
    import email_intake_service as mailsvc
    try:
        return mailsvc.carrier_cc(s, tenant_id)
    except Exception:  # noqa: BLE001
        return None


def _route_dict(r: IntakeRoute, broker_name: Optional[str],
                files_this_month: int = 0,
                program_name: Optional[str] = None,
                sftp: Optional[dict] = None) -> dict:
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
        # An SFTP route that collects from someone else's server: its settings
        # and last check (sftp_pull.public_view) — never the password or key.
        "sftp": sftp,
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
        # One query for every pull route's settings; none at all when the
        # carrier has none (or before migration 35 — then it returns {}).
        pull = (sftp_pull.configs_for_tenant(s, tid)
                if any(svc.is_external_sftp(r) for r in rows) else {})
        routes = [_route_dict(r, names.get(r.broker_party_id), counts.get(r.id, 0),
                              _prog_names.get(getattr(r, "program_id", None)),
                              sftp=sftp_pull.public_view(pull.get(r.id)))
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
        # With the contracts each one's files can be written under, so the
        # dialog's example names one — and says whether it has to.
        broker_programmes: dict[str, list] = {}
        for pb in (s.query(ProgramBroker)
                   .filter(ProgramBroker.tenant_id == tid,
                           ProgramBroker.status == "active").all()):
            broker_programmes.setdefault(str(pb.broker_party_id), []).append(
                {"program_id": pb.program_id,
                 "name": prog_names.get(pb.program_id, f"Programme {pb.program_id}"),
                 "code": svc.programme_code(pb.program_id),
                 "contracts": [{"contract_id": c.id, "name": svc.contract_label(c),
                                "code": svc.contract_code(c)}
                               for c in svc.live_contracts(s, tid, pb.program_id,
                                                           pb.broker_party_id)]})
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
        # The broker's ADMINS only: its users (operators) belong to the broker
        # alone and are never shown to the carrier.
        broker_emails: dict[str, list] = {}
        if broker_ids:
            from auth_deps import db_role_values
            for u in (s.query(AppUser)
                      .filter(AppUser.broker_party_id.in_(list(broker_ids)),
                              AppUser.role.in_(db_role_values("broker_admin")))
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
            # Who brokers copy (Cc) on every bordereau email to this carrier.
            "carrier_cc": _carrier_cc(s, tid),
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


@router.post("/sftp/test")
def test_sftp(body: SftpTestBody,
              principal: Principal = Depends(require_role("carrier_admin"))):
    """The Configure dialog's Test button for an external SFTP server.

    Connects, shows the server's fingerprint (what the route will trust), lists
    the folder and checks that a collected file can be moved or deleted — with
    a tiny test file of our own, never a real one. Always 200: a server that
    cannot be reached is ok=false with plain words and an error_code, because
    "it did not work, and this is why" IS the answer.
    """
    return sftp_pull.test_connection(body.model_dump())


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

    # SFTP from someone else's server: test it again — against the fingerprint
    # the carrier was shown, which is what gets pinned — before anything is
    # written, and outside the database session so a slow server never holds a
    # connection open.
    pull_address, pull_cfg = None, None
    if body.channel == "sftp" and body.sftp is not None:
        if not sftp_pull.column_ready(fresh=True):
            raise HTTPException(503, "Collecting from an SFTP server needs database update 35 "
                                     "(migrations/35_intake_route_sftp_config.sql). Ask your "
                                     "administrator to run it, then try again.")
        try:
            pull_address, pull_cfg = sftp_pull.prepare_route(body.sftp.model_dump())
        except sftp_pull.SetupRefused as exc:
            raise HTTPException(400, str(exc))

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
        elif pull_address is not None:
            # "sftp://user@host:port/folder" — the server we collect from.
            address = pull_address
        else:
            address = svc.build_sftp_address(carrier_name, broker.legal_name)

        # One API channel per broker + programme (uq_intake_route_api_scope):
        # say so, rather than let the insert fail with a server error.
        if body.channel == "api":
            twin = (s.query(IntakeRoute)
                    .filter(IntakeRoute.tenant_id == tid, IntakeRoute.channel == "api",
                            IntakeRoute.broker_party_id == broker.id,
                            IntakeRoute.program_id.is_(None) if body.program_id is None
                            else IntakeRoute.program_id == body.program_id).first())
            if twin is not None:
                raise HTTPException(409, f"{broker.legal_name} already has an API channel "
                                         "for this. Make a new key on it instead.")
        existing = None
        if body.channel != "api":
            existing = (s.query(IntakeRoute)
                        .filter(IntakeRoute.tenant_id == tid,
                                IntakeRoute.channel == body.channel,
                                IntakeRoute.address == address).first())
        if existing is not None:
            # UNIQUE (tenant_id, channel, address) would raise anyway; saying
            # which broker already holds it is more use than a 500.
            raise HTTPException(409, f"This channel already exists for "
                                     f"{broker.legal_name}: {address}")

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
        if pull_cfg is not None:
            # Raw SQL, same transaction: the column is never mapped on the
            # model (see sftp_pull), so a database without it still works.
            sftp_pull.save_config(s, route.id, pull_cfg)

        # Create the folders now, not on first file. A broker given an address
        # will test it immediately, and an SFTP put into a folder that does not
        # exist fails with a permission error that looks like a credential
        # problem — the hardest kind of support call to answer.
        created_dir = None
        if route.channel == "sftp" and pull_cfg is None:
            try:
                created_dir = str(svc.ensure_route_dirs(route))
            except OSError as exc:
                raise HTTPException(500, f"could not create the folder: {exc}")

        s.commit()
        s.refresh(route)
        _log(_tenant_name(s, tid) or "", _actor(principal), "intake_route_created",
             target=str(route.id),
             details={"channel": route.channel, "address": route.address,
                      "broker_party_id": route.broker_party_id,
                      # Which server key was trusted, and by whom (the actor).
                      **({"host_key": pull_cfg["host_key"]["fingerprint_sha256"]}
                         if pull_cfg else {})})
        out = _route_dict(route, broker.legal_name, 0,
                          sftp=sftp_pull.public_view(pull_cfg))
        out["folder"] = created_dir
        out["cc"] = _carrier_cc(s, tid) if route.channel == "email" else None
        # The broker is told how to name what they send — the same example
        # the dialog shows — so their first file is not a refused one.
        # API: emailed with its key, the moment the key is minted (create_key).
        out["guide"] = (_email_guide(s, route, carrier_name)
                        if route.channel != "api" else None)
        return out


def _email_guide(s, route: IntakeRoute, carrier: str, *,
                 api_key: Optional[str] = None, api_base: Optional[str] = None) -> dict:
    """Email the broker this channel's "How They Send It". Never fails the
    request that asked for it: the dialog still shows the example to copy."""
    import intake_guide
    try:
        return intake_guide.send(s, route, carrier=carrier, send_to=_send_to(route),
                                 api_key=api_key, api_base=api_base)
    except Exception:  # noqa: BLE001
        log.warning("could not email the channel guide for route %s", route.id, exc_info=True)
        return {"recipients": [], "sending": False}


@router.post("/routes/{route_id}/guide")
def email_route_guide(route_id: int, mga: Optional[str] = None,
                      principal: Principal = Depends(require_role("carrier_admin"))):
    """Send the broker this channel's instructions again."""
    with SessionLocal() as s:
        route = s.get(IntakeRoute, route_id)
        if route is None:
            raise HTTPException(404, "route not found")
        assert_tenant_owns(principal, route.tenant_id)
        if route.channel not in ("email", "sftp"):
            raise HTTPException(400, "only email and SFTP channels have instructions to send")
        return _email_guide(s, route, _tenant_name(s, route.tenant_id) or "your carrier")


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
                           svc.month_counts(s, route.tenant_id).get(route.id, 0),
                           sftp=(sftp_pull.public_view(sftp_pull.load_config(s, route.id))
                                 if svc.is_external_sftp(route) else None))


# What an arrival that has not been run reports. Keys match _run_facts.
_NO_RUN = {"run_result": None, "run_state": None, "run_error": None, "run_at": None,
           "run_export_id": None, "run_exception_count": None, "run_rows": None,
           "contract_id": None, "contract_name": None, "submitted_by_name": None,
           "reporting_period": None,
           "resolved_by_name": None}


def _run_result(state: Optional[str], export_status: Optional[str],
                exception_count: Optional[int]) -> Optional[str]:
    """One word for what the run did, in the Files screen's vocabulary:
    ingested · exceptions · not_checked · failed · not_run. None while it has
    not been run (or is being run right now)."""
    if state == "failed":
        return "failed"
    if state == "not_run":
        return "not_run"
    if state != "done":
        return None
    if export_status == "not_validated":
        return "not_checked"
    if export_status == "clean" or not exception_count:
        return "ingested"
    return "exceptions"


def _run_facts(s, rows) -> dict:
    """{arrival_id: run facts} for a page of arrivals — three small queries,
    never the export's multi-MB payload columns."""
    from db import Contract, OutputExport
    exp_ids = {a.run_export_id for a in rows if a.run_export_id}
    exports = {}
    if exp_ids:
        exports = {e.id: e for e in s.query(
            OutputExport.id, OutputExport.status, OutputExport.exception_count,
            OutputExport.policy_count, OutputExport.contract_id,
        ).filter(OutputExport.id.in_(exp_ids)).all()}
    # The run's contract, else the one the file arrived for (picked, sent,
    # named or the only one) — so a file shows its contract before it is run.
    con_ids = ({e.contract_id for e in exports.values() if e.contract_id}
               | {a.contract_id for a in rows if getattr(a, "contract_id", None)})
    contracts = {}
    if con_ids:
        contracts = {c.id: (c.name or c.filename or f"Contract #{c.id}")
                     for c in s.query(Contract.id, Contract.name, Contract.filename)
                     .filter(Contract.id.in_(con_ids)).all()}
    # Who uploaded it and who released or discarded it — one lookup for both.
    user_ids = {uid for a in rows
                for uid in (a.submitted_by_user_id, a.resolved_by_user_id) if uid}
    users = {}
    if user_ids:
        users = {u.id: (u.full_name or u.email)
                 for u in s.query(AppUser).filter(AppUser.id.in_(user_ids)).all()}
    # The reporting period each file was FOR. A file landed with migration 32
    # carries it; any other run says so through the calendar version its
    # export filled in.
    period_of = {}
    if exp_ids:
        from db import SubmissionVersion
        period_of = {eid: p for eid, p in
                     s.query(SubmissionVersion.received_export_id, SubmissionVersion.period)
                     .filter(SubmissionVersion.received_export_id.in_(exp_ids)).all() if p}
    out = {}
    for a in rows:
        e = exports.get(a.run_export_id)
        cid = (e.contract_id if e else None) or getattr(a, "contract_id", None)
        out[a.id] = {
            "reporting_period": (getattr(a, "reporting_period", None)
                                 or period_of.get(a.run_export_id)),
            "run_result": _run_result(a.run_state, e.status if e else None,
                                      e.exception_count if e else None),
            "run_state": a.run_state,
            "run_error": a.run_error,
            "run_at": _iso_utc(a.run_at),
            "run_export_id": a.run_export_id,
            "run_exception_count": e.exception_count if e else None,
            "run_rows": e.policy_count if e else None,
            "contract_id": cid,
            "contract_name": contracts.get(cid),
            "submitted_by_name": users.get(a.submitted_by_user_id),
            "resolved_by_name": users.get(a.resolved_by_user_id),
        }
    return out


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
        runs = _run_facts(s, rows)

        out = []
        for a in rows:
            route = routes.get(a.route_id)
            # A manual upload has no route; it carries its own channel and
            # programme (migration 29).
            prog_id = (getattr(route, "program_id", None) if route else None) or a.program_id
            out.append({
                "arrival_id": a.id,
                "filename": a.filename,
                "channel": route.channel if route else a.channel,
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
                "program_id": prog_id,
                "program_name": prog_names.get(prog_id),
                "received_at": _iso_utc(a.received_at),
                "outcome": a.outcome,
                "turned_away_reason": a.turned_away_reason,
                # SFTP has no reply path — a file dropped in a folder nobody owns
                # has nobody to tell. The screen says so rather than showing blank.
                "sender_notified_at": _iso_utc(a.sender_notified_at),
                "sender_notified_via": a.sender_notified_via,
                "bdx_upload_id": a.bdx_upload_id,
                "public_ref": a.public_ref,
                # Which submission this file is a version of (migration 32) —
                # the screen shows a submission once, as its latest file.
                "submission_ref": a.submission_ref,
                "version_no": a.version_no,
                # The period the file is for — with programme and contract,
                # what makes a later file the next version of this one.
                "reporting_period": a.reporting_period,
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
                # ── migration 29 — what became of it once it was run ──
                **runs.get(a.id, _NO_RUN),
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


@router.post("/arrivals/{arrival_id}/rerun")
def rerun_arrival(arrival_id: int,
                  principal: Principal = Depends(require_role("carrier_admin"))):
    """Run a file that went through again — after its run failed, after it could
    not be run automatically, or when it arrived before auto-run existed.

    It does not run here: it goes back in the auto-run queue (run_state NULL),
    so a retry takes exactly the path the first run took. The failed attempt's
    landing record stays; the new run gets its own."""
    with SessionLocal() as s:
        arrival = _arrival_for_review(s, arrival_id, principal)
        if arrival.outcome != "accepted":
            raise HTTPException(400, "Only accepted files can be processed.")
        if arrival.run_state not in ("failed", "not_run", "pre_autorun"):
            raise HTTPException(
                400, "This file is being run now." if arrival.run_state == "running"
                else "This file has already been run.")
        arrival.run_state = None
        arrival.run_error = None
        s.commit()
        _log(_tenant_name(s, arrival.tenant_id) or "", _actor(principal),
             "intake_arrival_rerun", target=str(arrival.id),
             details={"filename": arrival.filename})
    import intake_autorun
    intake_autorun.wake()
    return {"arrival_id": arrival_id, "run_state": None}


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
        # Released means "load it": auto-run takes it now rather than next tick.
        import intake_autorun
        intake_autorun.wake()
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
        elif svc.is_external_sftp(route):
            # Someone else's server: log in and collect now (sftp_pull).
            from sftp_pull import collect_route
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
def create_key(route_id: int, body: KeyCreate, request: Request,
               principal: Principal = Depends(require_role("carrier_admin"))):
    """Mint a key. The plaintext is returned exactly ONCE and never stored —
    the screen has to say so plainly before it disappears."""
    with SessionLocal() as s:
        route = s.get(IntakeRoute, route_id)
        if route is None:
            raise HTTPException(404, "route not found")
        assert_tenant_owns(principal, route.tenant_id)
        if route.channel != "api":
            raise HTTPException(400, "Only API channels have keys.")

        live = (s.query(IntakeCredential)
                .filter(IntakeCredential.route_id == route_id,
                        IntakeCredential.revoked_at.is_(None)).count())
        if live >= MAX_LIVE_KEYS:
            raise HTTPException(409, f"This channel already has {live} active keys "
                                     "— revoke one before creating another.")

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
        # The broker is emailed the key with how to use it — now, the only
        # moment the plaintext exists. The address is this API's own.
        from intake_api_routes import _base_url
        base = (os.getenv("API_PUBLIC_URL") or "").strip() or _base_url(request)
        guide = _email_guide(s, route, _tenant_name(s, route.tenant_id) or "your carrier",
                             api_key=full, api_base=base)
        return {
            "credential_id": cred.id,
            "label": cred.label,
            "api_key": full,
            "warning": "Copy this now. It is not stored and cannot be shown again.",
            "guide": guide,
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
