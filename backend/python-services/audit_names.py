"""Names for the things audit rows point at — read time only.

An audit row stores whatever its writer had to hand: a request path
("/contracts/7101/request-changes"), a typed id ("program:1295"), a bare number
whose meaning depends on the event ("2419" is a person on a password reset and a
party on party_created), and a details dict full of ids ("program id 1295 ·
format id 191"). None of that is what somebody reading the trail recognises.

This module turns each of them into a NAME, and says what KIND of thing it is
("Contract", "Programme", "Received file"), so the Target column answers "done
to what?" and the "what exactly" line reads as a sentence rather than as keys.

Nothing here writes, and nothing changes what is stored. The same rows simply
read better — including every row written before this existed. Lookups are
memoised per response and prefetched in bulk (one query per kind per page), so
a 20,000-row export does not become 20,000 round trips.

Naming matches audit_feed.ActorNamer: a broker's own admin is named; only a
retired broker-user seat named as a TARGET reads to a carrier as its company.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# what kind of thing
# ---------------------------------------------------------------------------

KIND_WORDS = {
    "contract":   "Contract",
    "program":    "Programme",
    "broker":     "Broker",
    "party":      "Party",
    "tenant":     "Carrier",
    "tenant_code": "Carrier",
    "pipeline":   "Bordereau Setup",
    "format":     "Bordereau Setup",
    "template":   "Output template",
    "mapper":     "Column mapping",
    "route":      "Ingestion channel",
    "key":        "Channel API key",
    "arrival":    "Received file",
    "submission": "Received file",
    "export":     "Processed bordereau",
    "output":     "Output BDX file",
    "landing":    "Uploaded file",
    "upload":     "Uploaded file",
    "user":       "User",
    "invitation": "Broker invitation",
    "envelope":   "Signing envelope",
    "rule":       "Rule",
    "clause":     "Contract clause",
    "task":       "Data-mapping task",
    "expected":   "Due submission",
    "request":    "Broker request",
    "wording":    "Contract wording",
    "file":       "File",
}

# A request path's collection segment -> the kind of id that follows it.
_COLLECTION = {
    "contracts": "contract", "programs": "program", "carriers": "tenant",
    "brokers": "broker", "envelopes": "envelope", "routes": "route",
    "arrivals": "arrival", "keys": "key", "invitations": "invitation",
    "broker-invitations": "invitation", "pipelines": "pipeline",
    "output-template": "template", "template": "template", "bordereau": "expected",
    "rule-library": "rule", "users": "user", "parties": "party",
    "downloads": "export", "mapper": "mapper", "mappers": "mapper",
    "format": "format", "mapping-tasks": "task", "uploads": "upload",
    "landing": "landing", "clause-routing": "clause",
    "broker-onboarding-requests": "request", "pages": "wording",
    "runs": "export",
}

# `kind:value` targets (program:1295, pipeline:216, submission:inb_…).
_PREFIX = {
    "program": "program", "contract": "contract", "pipeline": "pipeline",
    "format": "format", "template": "template", "task": "task", "clause": "clause",
    "party": "party", "broker": "broker", "submission": "submission",
    "route": "route", "user": "user", "export": "export", "landing": "landing",
    "mapper": "mapper", "tenant": "tenant", "envelope": "envelope", "rule": "rule",
}

# A bare-number target means whatever its EVENT means by it.
ACTION_TARGET_KIND = {
    "password_reset": "user", "password_changed": "user", "invite_resent": "user",
    "user_invited": "user", "profile_updated": "user", "user_updated": "user",
    "user_deleted": "user", "broker_user_created": "user",
    "party_created": "party", "party_updated": "party",
    "program_created": "program", "program_updated": "program",
    "submission_schedule_updated": "program",
    "rule_created": "rule", "rule_updated": "rule", "rule_deleted": "rule",
    "rule_enabled": "rule", "rule_disabled": "rule",
    "intake_route_created": "route", "intake_route_updated": "route",
    "intake_guide_emailed": "route", "intake_guide_sent": "route",
    "intake_route_contacts_updated": "route",
    "intake_arrival_rerun": "arrival",
    "intake_key_created": "key", "intake_key_revoked": "key",
    "contract_activated": "contract",
    "tenant_created": "tenant_code", "tenant_updated": "tenant_code",
    "intake.arrival.released": "arrival", "intake.arrival.discarded": "arrival",
}

# When the row's details carry the name outright, what kind of thing it names.
ACTION_NAME_KIND = {
    "contract_uploaded": "contract", "contract_awaiting_signature": "contract",
    "contract_pushed_back": "contract", "contract_sent_back": "contract",
    "contract_accepted": "contract", "contract_awaiting_review": "contract",
    "program_created": "program", "program_updated": "program",
    "party_created": "party", "party_updated": "party",
    "broker_added": "broker", "broker_linked": "broker",
    "broker_put_on_programme": "broker", "broker_request_submitted": "broker",
    "broker_request_approved": "broker", "broker_request_rejected": "broker",
    "broker_request_withdrawn": "broker",
    "bordereau_setup_activated": "pipeline", "bordereau_setup_submitted": "pipeline",
    "bordereau_setup_approved": "pipeline", "bordereau_setup_rejected": "pipeline",
    "bdx_setup_updated": "format", "bdx_setup_deleted": "format",
    "output_template_generated": "template", "output_template_updated": "template",
    "output_template_activated": "template", "output_template_refreshed": "template",
    "input_mapper_generated": "mapper", "input_mapper_updated": "mapper",
    "input_mapper_activated": "mapper",
    # The recorded name is the OUTPUT file ("Mahi_Corp_pvt_ltd_prg_test.xlsx"),
    # not the file the broker sent — said as such, or it reads as a second,
    # unknown "processed bordereau" beside the one named after the received file.
    "direct_output_generated": "output", "direct_output_checked": "output",
    "output_generated": "output",
    "user_invited": "user", "user_updated": "user", "user_deleted": "user",
    "intake.arrival.released": "arrival", "intake.arrival.discarded": "arrival",
    "intake_arrival_rerun": "arrival",
    "rule_enabled": "rule", "rule_disabled": "rule", "rule_created": "rule",
    "rule_updated": "rule", "rule_deleted": "rule",
}

# Paths that are not a record at all.
_WHOLE = {"/dwh": "The data model", "/audit/export": "The audit trail"}

_INB = re.compile(r"^inb_[0-9A-Z]+$")
_DIGITS = re.compile(r"^\d+$")


# ---------------------------------------------------------------------------
# small words
# ---------------------------------------------------------------------------

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def period_words(value: Any) -> str:
    """"2026-08" -> "Aug 2026". Anything else is returned as it was."""
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})$", text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return text


def date_words(value: Any) -> str:
    """"2026-09-10" (or a datetime) -> "10 Sep 2026"."""
    if isinstance(value, (datetime, date)):
        return f"{value.day} {_MONTHS[value.month - 1]} {value.year}"
    text = str(value or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(3))} {_MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return text


def field_words(name: Any) -> str:
    """A stored field name as a person says it: contract_id -> contract."""
    text = str(name or "").strip()
    text = re.sub(r"_ids?$", "", text)
    return text.replace("_", " ").replace(".", " ").strip()


def _clip(value: Any, n: int = 80) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= n else text[: n - 1] + "…"


def _strip_doc_ext(name: str) -> str:
    return re.sub(r"\.(pdf|docx?|rtf|txt)$", "", name, flags=re.I) or name


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------

class Names:
    """Ids -> names for one response. Build once, `prefetch` the page, then ask.

    An entry is {"name": str, "ctx": [ref, ...], "broker": party id or None}.
    `ctx` holds refs, not words, so a contract's programme is looked up through
    the same memo as every other programme on the page.
    """

    def __init__(self, s, viewer=None):
        self.s = s
        self.v = viewer
        self._memo: dict[tuple[str, Any], Optional[dict]] = {}

    # --- loading -----------------------------------------------------------
    def prefetch(self, refs: Iterable[tuple[str, Any]]) -> None:
        """Load every ref in bulk: one query per kind, then their context."""
        for _round in range(2):
            want: dict[str, set] = {}
            for kind, key in refs:
                if (kind, key) not in self._memo and kind in _LOADERS:
                    want.setdefault(kind, set()).add(key)
            for kind, keys in want.items():
                self._load(kind, keys)
            # Second round: what the first round's entries point at.
            refs = [c for e in list(self._memo.values()) if e
                    for c in [*e.get("ctx", ()), e.get("alias")] if isinstance(c, tuple)]

    def _load(self, kind: str, keys: set) -> None:
        keys = [k for k in keys if k is not None]
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            try:
                found = _LOADERS[kind](self.s, chunk)
            except Exception:  # noqa: BLE001 — wording must never break a read
                try:
                    self.s.rollback()
                except Exception:  # noqa: BLE001
                    pass
                found = {}
            for k in chunk:
                self._memo[(kind, k)] = found.get(k)

    def entry(self, kind: str, key: Any) -> Optional[dict]:
        if (kind, key) not in self._memo:
            if kind not in _LOADERS:
                return None
            self._load(kind, {key})
        e = self._memo.get((kind, key))
        # An invitation IS its broker and an envelope IS its contract: read
        # them through that record, keeping their own context after it.
        if e and e.get("name") is None and e.get("alias"):
            base = self.entry(*e["alias"]) or {}
            e = dict(e, name=base.get("name") or e.get("fallback") or "\u2014",
                     ctx=list(base.get("ctx", [])) + list(e.get("ctx", [])))
        return e

    # --- words -------------------------------------------------------------
    def name(self, kind: str, key: Any) -> Optional[str]:
        """The name of one thing, masked for this viewer. None if unknown."""
        e = self.entry(kind, key)
        if not e:
            return None
        # A retired broker-user seat ("operator"), named to a carrier: the
        # company. A broker's own admin is named — as the Actor column does.
        if kind == "user" and e.get("broker") and self.v is not None \
                and getattr(self.v, "is_carrier", False) \
                and e.get("role") not in (None, "broker_admin"):
            return self.name("broker", e["broker"]) or "Broker"
        return e.get("name")

    def ctx_names(self, kind: str, key: Any) -> list[str]:
        e = self.entry(kind, key)
        out = []
        for c in (e or {}).get("ctx", ()):
            if isinstance(c, tuple):
                n = self.name(*c)
                if n:
                    out.append(n)
            elif c:
                out.append(str(c))
        return out


# --- one loader per kind: (session, keys) -> {key: entry} -------------------

def _load_contract(s, keys):
    from db import Contract
    rows = (s.query(Contract.id, Contract.name, Contract.filename, Contract.program_id)
            .filter(Contract.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": _strip_doc_ext((r.name or r.filename or f"Contract #{r.id}").strip()),
                   "ctx": [("program", r.program_id)] if r.program_id else []}
            for r in rows}


def _load_program(s, keys):
    from db import Program
    rows = s.query(Program.id, Program.name).filter(Program.id.in_([int(k) for k in keys])).all()
    return {r.id: {"name": r.name or f"Programme #{r.id}", "ctx": []} for r in rows}


def _load_party(s, keys):
    from db import Party
    rows = s.query(Party.id, Party.legal_name).filter(Party.id.in_([int(k) for k in keys])).all()
    return {r.id: {"name": r.legal_name or f"#{r.id}", "ctx": []} for r in rows}


def _load_tenant(s, keys):
    from db import Tenant
    rows = (s.query(Tenant.id, Tenant.legal_name, Tenant.tenant_name)
            .filter(Tenant.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": r.legal_name or r.tenant_name, "ctx": []} for r in rows}


def _load_tenant_code(s, keys):
    from db import Tenant
    rows = (s.query(Tenant.tenant_name, Tenant.legal_name)
            .filter(Tenant.tenant_name.in_([str(k) for k in keys])).all())
    return {r.tenant_name: {"name": r.legal_name or r.tenant_name, "ctx": []} for r in rows}


def _named(model, label):
    def load(s, keys):
        rows = s.query(model.id, model.name).filter(model.id.in_([int(k) for k in keys])).all()
        return {r.id: {"name": r.name or f"{label} #{r.id}", "ctx": []} for r in rows}
    return load


def _load_pipeline(s, keys):
    from db import Pipeline
    return _named(Pipeline, "Bordereau Setup")(s, keys)


def _load_format(s, keys):
    from db import DirectFormat
    return _named(DirectFormat, "Bordereau Setup")(s, keys)


def _load_template(s, keys):
    from db import ExportTemplate
    ids = [int(k) for k in keys if _DIGITS.match(str(k))]
    if not ids:
        return {}
    return _named(ExportTemplate, "Output template")(s, ids)


def _load_mapper(s, keys):
    from db import Mapper
    rows = (s.query(Mapper.id, Mapper.name, Mapper.source_filename)
            .filter(Mapper.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": r.name or r.source_filename or f"Mapping #{r.id}", "ctx": []}
            for r in rows}


def _load_task(s, keys):
    from db import AdminMappingTask
    rows = (s.query(AdminMappingTask.id, AdminMappingTask.title)
            .filter(AdminMappingTask.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": r.title or f"Task #{r.id}", "ctx": []} for r in rows}


_CHANNEL_WORDS = {"email": "Email", "sftp": "SFTP", "api": "API", "upload": "Upload",
                  "cloud_folder": "Cloud folder"}


def _load_route(s, keys):
    from intake_models import IntakeRoute
    rows = (s.query(IntakeRoute.id, IntakeRoute.channel, IntakeRoute.address,
                    IntakeRoute.display_name, IntakeRoute.broker_party_id)
            .filter(IntakeRoute.id.in_([int(k) for k in keys])).all())
    out = {}
    for r in rows:
        how = _CHANNEL_WORDS.get(r.channel, (r.channel or "Channel").title())
        # An email route's address is who the broker sends FROM — the thing
        # that tells two of them apart. An API route's is the endpoint, the
        # same for everyone, and an SFTP folder is named after the broker.
        ctx: list = [r.address] if r.channel == "email" and r.address else []
        if r.broker_party_id:
            ctx.append(("broker", r.broker_party_id))
        out[r.id] = {"name": f"{how} channel", "ctx": ctx}
    return out


def _load_key(s, keys):
    from intake_models import IntakeCredential
    rows = (s.query(IntakeCredential.id, IntakeCredential.route_id,
                    IntakeCredential.label, IntakeCredential.last4)
            .filter(IntakeCredential.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": (r.label or "API key") + (f" ending {r.last4}" if r.last4 else ""),
                   "ctx": [("route", r.route_id)] if r.route_id else []} for r in rows}


def _arrival_entry(r) -> dict:
    ctx: list = []
    if r.program_id:
        ctx.append(("program", r.program_id))
    if r.reporting_period:
        ctx.append(period_words(r.reporting_period))
    if r.version_no:
        ctx.append(f"Version {r.version_no}")
    return {"name": r.filename or "file", "ctx": ctx}


def _arrival_cols():
    from intake_models import FileArrival as A
    return (A.id, A.filename, A.program_id, A.reporting_period, A.version_no,
            A.public_ref, A.submission_ref)


def _load_arrival(s, keys):
    from intake_models import FileArrival as A
    ids = [int(k) for k in keys if _DIGITS.match(str(k))]
    refs = [str(k) for k in keys if not _DIGITS.match(str(k))]
    out = {}
    if ids:
        for r in s.query(*_arrival_cols()).filter(A.id.in_(ids)).all():
            out[r.id] = _arrival_entry(r)
    if refs:
        for r in s.query(*_arrival_cols()).filter(A.public_ref.in_(refs)).all():
            out[r.public_ref] = _arrival_entry(r)
    return out


def _load_submission(s, keys):
    """inb_… -> the file as the broker last sent it. A submission is one file
    over several versions; its newest name is the one people recognise."""
    from intake_models import FileArrival as A
    out = {}
    rows = (s.query(*_arrival_cols())
            .filter(A.submission_ref.in_([str(k) for k in keys]))
            .order_by(A.version_no.desc().nullslast(), A.id.desc()).all())
    for r in rows:
        if r.submission_ref not in out:
            e = _arrival_entry(r)
            # The version on THIS row is said by the row's own details.
            e["ctx"] = [c for c in e["ctx"] if not str(c).startswith("Version ")]
            out[r.submission_ref] = e
    return out


def _load_export(s, keys):
    """A processed bordereau, named after the file it was made from — every
    run on one programme produces an output with the same name, so the output
    name alone cannot tell two runs apart."""
    from db import LandingRecord, OutputExport
    from intake_models import FileArrival as A
    ids = [int(k) for k in keys]
    rows = (s.query(OutputExport.id, OutputExport.filename, OutputExport.program_id,
                    OutputExport.version_no)
            .filter(OutputExport.id.in_(ids)).all())
    received = {}
    for r in (s.query(A.run_export_id, A.filename, A.reporting_period, A.version_no)
              .filter(A.run_export_id.in_(ids))
              .order_by(A.version_no.asc().nullsfirst()).all()):
        received[r.run_export_id] = r
    landed = {r.output_export_id: r.source_filename for r in
              s.query(LandingRecord.output_export_id, LandingRecord.source_filename)
              .filter(LandingRecord.output_export_id.in_(ids)).all()}
    out = {}
    for r in rows:
        got = received.get(r.id)
        name = (got.filename if got else None) or landed.get(r.id) or r.filename \
            or f"Run #{r.id}"
        ctx: list = [("program", r.program_id)] if r.program_id else []
        if got and got.reporting_period:
            ctx.append(period_words(got.reporting_period))
        ver = (got.version_no if got else None) or r.version_no
        if ver:
            ctx.append(f"Version {ver}")
        out[r.id] = {"name": name, "ctx": ctx}
    return out


def _load_landing(s, keys):
    from db import LandingRecord
    rows = (s.query(LandingRecord.id, LandingRecord.source_filename)
            .filter(LandingRecord.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": r.source_filename or f"Upload #{r.id}", "ctx": []} for r in rows}


def _load_upload(s, keys):
    from db import Upload
    rows = (s.query(Upload.id, Upload.source_file)
            .filter(Upload.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": r.source_file or f"Upload #{r.id}", "ctx": []} for r in rows}


def _load_user(s, keys):
    from db import AppUser
    rows = (s.query(AppUser.id, AppUser.full_name, AppUser.email, AppUser.broker_party_id,
                    AppUser.role)
            .filter(AppUser.id.in_([int(k) for k in keys])).all())
    out = {}
    for r in rows:
        name = r.full_name or r.email or f"User #{r.id}"
        if r.full_name and r.email:
            name = f"{r.full_name} ({r.email})"
        out[r.id] = {"name": name, "ctx": [], "broker": r.broker_party_id,
                     "role": r.role}
    return out


def _load_invitation(s, keys):
    from db import BrokerInvitation as B
    rows = (s.query(B.id, B.org_name, B.party_id, B.program_id)
            .filter(B.id.in_([int(k) for k in keys])).all())
    out = {}
    for r in rows:
        ctx = [("program", r.program_id)] if r.program_id else []
        if r.party_id:
            out[r.id] = {"name": None, "ctx": ctx, "alias": ("broker", r.party_id),
                         "fallback": r.org_name}
        else:
            out[r.id] = {"name": r.org_name or f"Invitation #{r.id}", "ctx": ctx}
    return out


def _load_envelope(s, keys):
    from db import EsignEnvelope as E
    rows = (s.query(E.id, E.title, E.contract_id)
            .filter(E.id.in_([int(k) for k in keys])).all())
    return {r.id: ({"name": None, "ctx": [], "alias": ("contract", r.contract_id),
                    "fallback": r.title} if r.contract_id
                   else {"name": r.title or f"Envelope #{r.id}", "ctx": []}) for r in rows}


def _load_rule(s, keys):
    from db import GenericRuleSpecification as G
    rows = (s.query(G.id, G.rule_name).filter(G.id.in_([int(k) for k in keys])).all())
    return {r.id: {"name": r.rule_name or f"Rule #{r.id}", "ctx": []} for r in rows}


def _load_expected(s, keys):
    from db import ExpectedSubmission as X
    rows = (s.query(X.id, X.program_id, X.period, X.broker_party_id)
            .filter(X.id.in_([int(k) for k in keys])).all())
    out = {}
    for r in rows:
        ctx: list = [period_words(r.period)] if r.period else []
        if r.broker_party_id:
            ctx.append(("broker", r.broker_party_id))
        out[r.id] = {"name": None, "ctx": ctx, "alias": ("program", r.program_id),
                     "fallback": f"Submission #{r.id}"}
    return out


_LOADERS = {
    "contract": _load_contract, "program": _load_program, "party": _load_party,
    "broker": _load_party, "tenant": _load_tenant, "tenant_code": _load_tenant_code,
    "pipeline": _load_pipeline, "format": _load_format, "template": _load_template,
    "mapper": _load_mapper, "task": _load_task, "route": _load_route, "key": _load_key,
    "arrival": _load_arrival, "submission": _load_submission, "export": _load_export,
    "landing": _load_landing, "upload": _load_upload, "user": _load_user,
    "invitation": _load_invitation, "envelope": _load_envelope, "rule": _load_rule,
    "expected": _load_expected,
}


# ---------------------------------------------------------------------------
# reading a target
# ---------------------------------------------------------------------------

def path_refs(path: str) -> list[tuple[str, Any]]:
    """Every (kind, id) a request path names, in order.

    /carriers/1377/programs/1295/brokers/1600/contracts/7520/runs
      -> tenant 1377, program 1295, broker 1600, contract 7520
    """
    segs = [p for p in path.split("?")[0].strip("/").split("/") if p]
    out: list[tuple[str, Any]] = []
    for i, seg in enumerate(segs):
        prev = segs[i - 1] if i else ""
        if _DIGITS.match(seg) and prev in _COLLECTION:
            out.append((_COLLECTION[prev], int(seg)))
        elif prev == "tenants" and seg not in ("new",):
            out.append(("tenant_code", seg))
    return out


def target_refs(action: str, target: Optional[str]) -> list[tuple[str, Any]]:
    """What a stored `target` points at, as refs. Empty when it names nothing."""
    t = (target or "").strip()
    if not t:
        return []
    if t.startswith("/"):
        return path_refs(t)
    if _DIGITS.match(t):
        kind = ACTION_TARGET_KIND.get(action)
        return [(kind, int(t))] if kind else []
    if _INB.match(t):
        return [("arrival", t)]
    if ":" in t:
        prefix, _, rest = t.partition(":")
        kind = _PREFIX.get(prefix)
        if kind == "submission":
            return [("submission", rest)]
        if kind and _DIGITS.match(rest):
            return [(kind, int(rest))]
        return []
    if ACTION_TARGET_KIND.get(action) == "tenant_code":
        return [("tenant_code", t)]
    return []


_FILE_KEYS = ("filename", "file", "source_filename")
_NAME_KEYS = _FILE_KEYS + ("name", "template_name")
_BROKER_ROLES = {"broker_admin", "operator", "broker_user"}

# With no record in the target, the details' own ids say what it was about —
# the most specific first.
_DETAIL_TARGET = (("contract_id", "contract"), ("program_id", "program"),
                  ("broker_party_id", "broker"))


def _recorded_name(names: Names, action: str, d: dict) -> tuple[Optional[str], Optional[str]]:
    """(name, kind) the row itself recorded, if any."""
    for k in _NAME_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            kind = ACTION_NAME_KIND.get(action)
            if kind is None and k in _FILE_KEYS:
                kind = "file"
            # A sample or supporting file is a FILE, whatever setup it belongs to.
            if k in _FILE_KEYS and action in ("direct_setup_uploaded", "supplement_uploaded",
                                              "bdx_uploaded"):
                kind = "file"
            return v.strip(), kind
    return None, None


def _person_recorded(names: Names, d: dict) -> Optional[str]:
    """A person the row named by email — standing in for one since deleted.

    Never a broker's own person to a carrier: when the row cannot show it was
    a carrier seat, a carrier viewer is told no more than "a user"."""
    if not (d.get("full_name") or d.get("email")):
        return None
    if names.v is not None and getattr(names.v, "is_carrier", False):
        role = str(d.get("role") or "")
        if role in _BROKER_ROLES and role != "broker_admin":
            return "A broker user"
        if role not in ("carrier_admin", "carrier_user"):
            return "A user"
    who = d.get("full_name") or d.get("email")
    if d.get("full_name") and d.get("email"):
        who = f"{d['full_name']} ({d['email']})"
    return str(who)


def target_words(names: Names, action: str, target: Optional[str],
                 details: Any) -> tuple[Optional[str], str]:
    """(what kind of thing, its name and where it sits) for one row.

    The name the row itself recorded beats a lookup — it is what the thing was
    called WHEN it happened. A lookup fills in everything else.
    """
    d = details if isinstance(details, dict) else {}
    t = (target or "").strip()
    if t in _WHOLE:
        return None, _WHOLE[t]
    refs = target_refs(action, t)
    if not refs:
        for key, k in _DETAIL_TARGET:
            if d.get(key) is not None and _DIGITS.match(str(d[key])):
                refs = [(k, int(d[key]))]
                break
    primary = refs[-1] if refs else None

    recorded, rkind = _recorded_name(names, action, d)
    if recorded:
        kind = rkind or (primary[0] if primary else None)
        if kind == "contract":
            recorded = _strip_doc_ext(recorded)
        # The records the target points at are WHERE this sits — unless the
        # name IS that record, when its own context (a contract's programme)
        # is what follows.
        same = primary is not None and _same_kind(primary[0], kind)
        ctx = _context(names, refs, primary if same else None)
        # A direct run's target is "direct:<carrier> — <programme>": the
        # programme is where the output file belongs.
        if t.startswith("direct:") and " \u2014 " in t:
            prog = t.split(" \u2014 ", 1)[1].strip()
            if prog and prog not in ctx:
                ctx.append(prog)
        return (KIND_WORDS.get(kind) if kind else None,
                " · ".join([recorded] + [c for c in ctx if c != recorded]))

    if primary is None:
        if t.startswith("exceptions:"):
            n = t.partition(":")[2]
            return "Exceptions", f"{n} exception(s)"
        if not t or t.startswith("/"):
            return None, "—"
        if ":" in t:
            prefix, _, rest = t.partition(":")
            k = _PREFIX.get(prefix)
            return (KIND_WORDS.get(k) if k else None), (rest or t)
        kind = ACTION_NAME_KIND.get(action)
        return (KIND_WORDS.get(kind) if kind else None), t

    pk, key = primary
    shown = pk
    main = names.name(pk, key)
    if main is None:
        if pk == "user":
            main = _person_recorded(names, d) or f"User #{key}"
        elif pk == "wording":
            main = f"Contract wording, page {key}"
        else:
            main = f"{KIND_WORDS.get(pk, pk.title())} #{key}"
    ctx = _context(names, refs, primary)
    # A clause is only findable through its contract.
    if pk == "clause" and d.get("contract_id") is not None:
        n = names.name("contract", d["contract_id"])
        if n and n not in ctx:
            ctx.insert(0, n)
    return KIND_WORDS.get(shown), " · ".join([main] + [c for c in ctx if c != main])


def _same_kind(a: Optional[str], b: Optional[str]) -> bool:
    group = {"broker": "party", "party": "party", "format": "setup", "pipeline": "setup",
             "submission": "arrival", "arrival": "arrival"}
    return a is not None and b is not None and group.get(a, a) == group.get(b, b)


def _context(names: Names, refs: list, primary) -> list[str]:
    """Where the primary thing sits: the other records in its path (a broker's
    programme, a run's programme and broker), then its own context (a
    contract's programme, a file's period and version). The carrier is left
    out when anything else is known — the Carrier column already says it.
    `primary=None` means every ref is context."""
    out: list[str] = []
    others = [r for r in refs if r != primary]
    if len(refs) > 1 or primary is None:
        others = [r for r in others if r[0] not in ("tenant", "tenant_code")] or others
    for k, key in others:
        n = names.name(k, key)
        if n and n not in out:
            out.append(n)
    if primary:
        for n in names.ctx_names(*primary):
            if n not in out:
                out.append(n)
    return out


# ---------------------------------------------------------------------------
# reading the details — "what exactly"
# ---------------------------------------------------------------------------

# Never worth a reader's time: the HTTP status is the badge, the IP has its
# own column, the actor's email is the actor, a name is the Target, and the
# rest are internal plumbing (signed-link ids, outbox keys, scope flags).
_SKIP = {"status", "ip", "method", "email", "full_name", "user_agent",
         "filename", "file", "name", "template_name", "source_filename",
         "link", "key", "target", "via", "reference", "submission_ref",
         "public_ref", "scope", "link_status", "lifecycle", "event", "channel",
         "known_format", "datamodel_mapped", "output_fields", "rule_ids",
         "carrier_party_id", "tenant_id", "expected_id", "check_export_id",
         "format_id", "template_id", "output_template_id", "pipeline_id",
         "landing_id", "mapper_id", "task_id", "request_id", "export_id",
         "upload_id", "rule_id", "arrival_id", "route_id", "credential_id",
         "person_added_later"}

# Ids worth reading — as the name of what they point at.
_ID_KIND = {"program_id": "program", "contract_id": "contract",
            "broker_party_id": "broker", "party_id": "party", "user_id": "user"}
# The *_name key that already says the same thing as one of those ids.
_ID_NAME = {"program_id": "program_name", "broker_party_id": "broker_name",
            "contract_id": "contract_name", "party_id": "legal_name"}

_KIND_LABEL = {"program": "Programme", "contract": "Contract", "broker": "Broker",
               "party": "Party", "user": "User"}

_NAME_LABEL = {"program_name": "Programme", "broker_name": "Broker",
               "contract_name": "Contract", "legal_name": None, "rule_name": "Rule",
               "org_name": "Broker", "party_type": "Type"}

_COUNT_LABEL = {
    "rows": "{v} rows", "row_count": "{v} rows", "policies": "{v} policies",
    "exceptions": "{v} exceptions", "updated": "{v} updated", "skipped": "{v} skipped",
    "loaded": "{v} loaded", "failed": "{v} failed", "sheet_count": "{v} sheets",
    "count": "{v} items", "resolved": "{v} resolved", "saved": "{v} answers saved",
    "rule_count": "{v} rules", "total": "{v} in total", "queued": "{v} queued",
    "unresolved_count": "{v} exceptions unresolved",
}

_BOOL_WORDS = {
    "mail_sent": ("email sent", "email not sent"),
    "approved": ("approved", None),
    "reactivated": ("re-activated", None),
    "flagged": ("with open exceptions", None),
    "is_enabled": ("switched on", "switched off"),
    "is_active": ("switched on", "switched off"),
    "auto_ingest": ("loads automatically", "does not load automatically"),
}

_TEXT_LABEL = {
    "reason": "{v}", "held_because": "Was held: {v}", "note": "Note: {v}",
    "error": "Error: {v}", "mail_error": "Email not sent: {v}",
    "recipient": "To {v}", "role": "Role {v}", "stage": "Stage {v}",
    "lane": "{v} lane", "action": "Decision: {v}", "file_style": "File style: {v}",
    "display_name": "Named {v}", "output_field": "Field {v}",
    "via_secure_link": "{v}",
}

_DECISION_WORDS = {"approve": "Approved", "reject": "Rejected", "dismiss": "Dismissed"}

_ROLE_WORDS = {"carrier_admin": "Carrier", "broker_admin": "Broker",
               "kavachio_admin": "Kavachio Admin", "operator": "Broker User",
               "carrier_user": "Carrier User"}

# The broker's file emails, as their subject line says them.
NOTICE_WORDS = {
    "with_exceptions": "Exceptions to resolve", "duplicate": "File already received",
    "delivered": "Delivered", "delivered_flagged": "Delivered with open exceptions",
    "failed": "File could not be processed", "rejected": "File rejected",
    "on_hold": "File on hold", "clean": "Clean", "ready": "Ready to deliver",
    "held_at_deadline": "Deadline passed — file on hold",
    "carrier_deadline": "Decision needed: file on hold",
    "deadline_hold": "Deadline passed — file on hold",
}


def _values(v) -> str:
    if isinstance(v, (list, tuple, set)):
        # A list of records is counted, never printed as raw data.
        if any(isinstance(x, (dict, list, tuple)) for x in v):
            return f"{len(v)} items"
        return ", ".join(str(x) for x in v if x not in (None, ""))
    return str(v)


def _generic(names: Names, d: dict) -> list[tuple[str, Optional[str]]]:
    """(phrase, the entity name in it or None) for each key worth reading."""
    parts: list[tuple[str, Optional[str]]] = []
    for key, value in d.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if key in _ID_KIND:
            if d.get(_ID_NAME.get(key, "")):
                continue                       # the *_name says it already
            n = names.name(_ID_KIND[key], value) if not isinstance(value, (list, dict)) else None
            if n:
                parts.append((f"{_KIND_LABEL[_ID_KIND[key]]} {n}", n))
            continue
        if key in _SKIP or key.endswith("_id") or key.endswith("_ids"):
            continue
        if key in _NAME_LABEL:
            label = _NAME_LABEL[key]
            text = _values(value)
            parts.append((f"{label} {_clip(text)}" if label else _clip(text), text))
            continue
        if key == "rule_names":
            parts.append((f"Rules: {_clip(_values(value), 100)}", None))
            continue
        if key in ("period", "reporting_period"):
            parts.append((f"Period {period_words(value)}", None))
            continue
        if key.endswith("_date") or key in ("due", "deadline_at", "released_on"):
            label = field_words(key.replace("_date", "")).capitalize()
            parts.append((f"{label} {date_words(value)}", None))
            continue
        if key in ("version", "version_no"):
            parts.append((f"Version {value}", None))
            continue
        if key == "changed":
            parts.append(("Changed: " + _clip(", ".join(field_words(x) for x in
                          (value if isinstance(value, (list, tuple)) else [value])), 100), None))
            continue
        if key in ("to", "cc", "emails"):
            label = {"to": "To", "cc": "Cc", "emails": "Contacts:"}[key]
            parts.append((f"{label} {_clip(_values(value), 100)}", None))
            continue
        if isinstance(value, bool):
            yes, no = _BOOL_WORDS.get(key, (None, None))
            word = yes if value else no
            if word:
                parts.append((word, None))
            continue
        if key == "role":
            parts.append((f"Role {_ROLE_WORDS.get(str(value), field_words(value))}", None))
            continue
        if key in _COUNT_LABEL:
            parts.append((_COUNT_LABEL[key].format(v=value), None))
            continue
        if key == "action" and str(value) in _DECISION_WORDS:
            parts.append((_DECISION_WORDS[str(value)], None))
            continue
        if key in _TEXT_LABEL:
            text = field_words(value) if key in ("file_style", "action") else _clip(value, 200)
            parts.append((_TEXT_LABEL[key].format(v=text), None))
            continue
        if isinstance(value, dict):
            inner = ", ".join(f"{v} {field_words(k)}" if isinstance(v, (int, float))
                              else f"{field_words(k)} {v}"
                              for k, v in value.items() if v not in (None, ""))
            if inner:
                parts.append((_clip(inner, 100), None))
            continue
        parts.append((f"{field_words(key).capitalize()} {_clip(_values(value))}", None))
    return parts


def _notice(names, d):
    """bdx_notice_*: to whom, about which version. Which email it was — its
    subject — is the Action words (audit_feed.notice_words)."""
    parts = []
    if d.get("recipient"):
        parts.append((f"to {d['recipient']}", None))
    if d.get("version"):
        parts.append((f"Version {d['version']}", None))
    if d.get("error"):
        parts.append((f"Error: {_clip(d['error'], 120)}", None))
    return parts


def _bordereau_sent(names, d):
    parts = []
    if d.get("version_no"):
        parts.append((f"Version {d['version_no']}", None))
    if d.get("period"):
        parts.append((f"Period {period_words(d['period'])}", None))
    if d.get("to"):
        parts.append((f"To {_clip(_values(d['to']), 80)}", None))
    if d.get("cc"):
        parts.append((f"Cc {_clip(_values(d['cc']), 80)}", None))
    if d.get("mail_sent") is False:
        parts.append(("Email not sent" + (f": {_clip(d['mail_error'], 80)}"
                                          if d.get("mail_error") else ""), None))
    return parts


def _fix_validated(names, d):
    """The review link's Validate: a preview of the next version, not sent."""
    parts = []
    if d.get("version"):
        parts.append((f"Preview of Version {d['version']} (not sent yet)", None))
    if d.get("corrected") is not None:
        ok = d.get("corrected_ok")
        parts.append((f"{d['corrected']} corrected" +
                      (f", {ok} now pass the checks" if ok is not None else ""), None))
    if d.get("still_failing_count"):
        parts.append((f"{d['still_failing_count']} still failing", None))
    if d.get("open_after") is not None:
        b = d.get("blocking_after")
        parts.append((f"{d['open_after']} exceptions still open" +
                      (f" ({b} must be fixed before delivery)" if b else ""), None))
    return parts


def _fix_link(names, d):
    """The review link's sign-in and submit rows: which link — the one emailed
    to this address — and which version of the file it was for."""
    parts = []
    email = d.get("email")
    if email:
        parts.append((f"Link emailed to {email}", None))
    if d.get("from_version"):
        to = d.get("to_version")
        parts.append((f"Corrections to Version {d['from_version']} sent in as "
                      + (f"Version {to}" if to else "the next version"), None))
    elif d.get("version"):
        parts.append((f"Version {d['version']}", None))
    return parts


def _fix_link_code(names, d):
    parts = []
    if d.get("email"):
        parts.append((f"Code emailed to {d['email']}", None))
    if d.get("version"):
        parts.append((f"Version {d['version']}", None))
    return parts


def _delivered(names, d):
    parts = []
    if d.get("version"):
        parts.append((f"Version {d['version']}", None))
    if d.get("flagged") and d.get("unresolved_count"):
        parts.append((f"{d['unresolved_count']} exceptions still open", None))
    return parts


def _mapping_proposed(names, d):
    st = d.get("stats") if isinstance(d.get("stats"), dict) else {}
    words = {"successful": "matched", "likely": "likely", "unsuccessful": "not matched"}
    got = [f"{v} {words.get(k, field_words(k))}" for k, v in st.items() if v not in (None, "")]
    return [(", ".join(got), None)] if got else []


_SPECIAL = {
    "bordereau_sent": _bordereau_sent,
    "bdx_fix_link_validated": _fix_validated,
    "bdx_fix_link_code_sent": _fix_link_code,
    "bdx_fix_link_code_wrong": _fix_link,
    "bdx_fix_link_opened": _fix_link,
    "bdx_fix_link_submitted": _fix_link,
    "bdx_fix_link_answers": _fix_link,
    "bdx_submission_delivered": _delivered,
    "datamodel_mapping_proposed": _mapping_proposed,
}


def detail_words(names: Names, action: str, details: Any, target_text: str = "") -> str:
    """The specifics of one event, in a phrase — names, not ids.

    Anything the Target column already says is left out, so the line adds to
    the row rather than repeating it.
    """
    d = details if isinstance(details, dict) else {}
    if action.startswith("bdx_notice_"):
        parts = _notice(names, d)
    elif action in _SPECIAL:
        parts = _SPECIAL[action](names, d)
    else:
        parts = _generic(names, d)
    out: list[str] = []
    for text, ent in parts:
        if not text:
            continue
        if ent and target_text and ent in target_text:
            continue
        if text not in out:
            out.append(text)
    return " · ".join(out[:6])


def row_refs(action: str, target: Optional[str], details: Any) -> list[tuple[str, Any]]:
    """Every ref one row will ask about — for prefetching a whole page."""
    refs = list(target_refs(action, target))
    d = details if isinstance(details, dict) else {}
    for key, kind in _ID_KIND.items():
        v = d.get(key)
        if v is not None and _DIGITS.match(str(v)):
            refs.append((kind, int(v)))
    return [r for r in refs if r[0] in _LOADERS]
