"""
change_history.py
─────────────────
Task 48.1 — for every change to an important record, keep what each field
was before and after, when, by whom, and why. The Audit Logs "Change History"
tab reads it back (read side at the bottom of this file); task 48.2's Rule
History (rule_history.py) and 49.3's "Draft changed → before / after" read the
same rows.

HOW A CHANGE IS CAUGHT
  One SQLAlchemy hook on every Session, so no screen or route had to change:
    before_flush           reads from the database any old value the code
                           never loaded (expired by a commit, or deferred) —
                           the attribute history has none for those;
    after_flush            reads each tracked object's attribute history (the
                           value loaded from the database and the one about to
                           replace it) and works out the field changes;
    after_flush_postexec   adds one activity_events row per changed record, in
                           the SAME transaction — a save that rolls back leaves
                           no history behind, and a history row can never
                           describe a change that did not happen.
  Contract checks (validation_rule) have no ORM model; rule_history.py
  catches their raw SQL the same way, one level down.

WHAT IS TRACKED (TRACKED below)
  programmes, companies and their contacts, users, carriers, contracts,
  brokers on programmes, Bordereau Setups, mappings (Bordereau Setup and
  data mapping), output templates, Rule Library rules and Programme Rules.
  Bookkeeping columns (timestamps, caches, AI samples, file blobs) are skipped,
  and anything that looks like a secret (password, token, hash, key) is never
  copied — a change to one is recorded as "changed", without either value.

WHERE IT IS KEPT
  activity_events, action "record_changed" — no new table, no migration.
  details = {entity, entity_id, name, module, event, changes: [{field, old,
  new}], reason, program_id, contract_id, broker_party_id, [rule], [before]}
  The Audit Trails tab leaves these rows out (audit_feed.NEVER_SHOWN_ACTIONS):
  the request that made the change already has its own line there.

WHO AND WHY
  ChangeContext (ASGI middleware, main.py) holds the signed-in user and the
  optional reason (the X-Change-Reason header the reason popup sends) for the
  length of one request. Only changes made inside a signed-in request are
  recorded: the daily sweep moving a status or the email poller landing a file
  are not a person editing a record, and their own named rows say what they did.

HISTORY STARTS WHEN THIS SHIPS
  Nothing is backfilled. Earlier edits stay in Audit Trails as "Edited a
  contract" etc., which never recorded the fields.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional
from urllib.parse import unquote

from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session

log = logging.getLogger("bdx.change_history")

ACTION = "record_changed"
REASON_HEADER = "x-change-reason"
MAX_CHANGES = 80          # per record per save; the rest are counted, not listed
CLIP = 300                # one value, as shown
CLIP_TEXT = 2000          # a contract wording section: long enough to compare drafts

# ── who is acting (one request) ────────────────────────────────────────────
_ctx: ContextVar[Optional[dict]] = ContextVar("kavachio_change_ctx", default=None)


def current() -> Optional[dict]:
    """The signed-in user and the reason for this request, or None."""
    return _ctx.get()


class ChangeContext:
    """Pure ASGI middleware: puts {user_id, tenant_id, reason} in a context
    variable for every writing request. Pure ASGI (not BaseHTTPMiddleware) so
    the variable reaches the endpoint, its thread and its background tasks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return
        token = _ctx.set(_context_from_headers(scope.get("headers") or []))
        try:
            await self.app(scope, receive, send)
        finally:
            _ctx.reset(token)


def _context_from_headers(headers) -> dict:
    import audit
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in headers}
    uid, tid = audit.actor_from_token(h.get("authorization"))
    return {"user_id": uid, "tenant_id": tid, "reason": clean_reason(h.get(REASON_HEADER))}


def clean_reason(raw: Optional[str]) -> Optional[str]:
    """The popup sends the reason URL-encoded (a header is Latin-1 only)."""
    if not raw:
        return None
    try:
        text_ = unquote(raw)
    except Exception:  # noqa: BLE001
        text_ = raw
    text_ = " ".join(text_.split())[:500]
    return text_ or None


# ── what a value looks like on screen ──────────────────────────────────────
_SECRET = re.compile(r"(password|passwd|secret|token|hash|otp|salt|private|credential|api_?key|pepper)", re.I)


def show(v: Any, clip: int = CLIP) -> Optional[str]:
    """One stored value as the words a person reads. None → None (shown "—")."""
    if v is None or v == "" or v == [] or v == {}:
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (Decimal, float)):
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:.6g}"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, sort_keys=True, default=str, ensure_ascii=False)
    else:
        s = str(v)
    s = s.strip()
    return (s[:clip - 1] + "…") if len(s) > clip else (s or None)


def words(key: str) -> str:
    """'inception_dt' → 'Inception date', 'broker_party_id' → 'Broker'."""
    k = re.sub(r"_(party_)?id$", "", key)
    k = re.sub(r"_dt$", "_date", k)
    k = k.replace("_", " ").strip()
    return (k[:1].upper() + k[1:]) if k else key


# ── JSON columns: what changed inside ──────────────────────────────────────
_LIST_KEYS = ("key", "field_key", "name", "field", "title", "sheet_name", "column_name", "id")


def leaves(v: Any, prefix: tuple = (), depth: int = 0, out: Optional[dict] = None) -> dict:
    """A JSON value as {path: leaf}. Lists of objects are keyed by their own
    name/key where they have one, so moving an item does not read as a change."""
    out = {} if out is None else out
    if isinstance(v, dict) and depth < 5:
        for k, x in v.items():
            leaves(x, prefix + (str(k),), depth + 1, out)
    elif isinstance(v, list) and depth < 5 and v and all(isinstance(i, dict) for i in v):
        seen: set = set()
        for n, item in enumerate(v, 1):
            key = next((str(item[k]) for k in _LIST_KEYS if item.get(k) not in (None, "")), str(n))
            if key in seen:
                key = f"{key} ({n})"
            seen.add(key)
            leaves(item, prefix + (key,), depth + 1, out)
    else:
        out[prefix] = v
    return out


def json_changes(label: str, old: Any, new: Any, *, path_words: Callable[[tuple], str] | None = None,
                 skip: Callable[[tuple], bool] | None = None, clip: int = CLIP) -> list[dict]:
    a, b = leaves(old), leaves(new)
    out = []
    for p in sorted(set(a) | set(b), key=lambda t: [str(x) for x in t]):
        if skip and skip(p):
            continue
        o, n = show(a.get(p), clip), show(b.get(p), clip)
        if o == n:
            continue
        where = path_words(p) if path_words else " › ".join(words(str(x)) for x in p)
        out.append({"field": f"{label} › {where}" if where else label, "old": o, "new": n})
    return out


# ── the records ────────────────────────────────────────────────────────────
@dataclass
class Track:
    entity: str                                   # "contract"
    module: str                                   # "Contracts" — the Module filter
    kind_words: str                               # "Contract" — the What column
    name: Callable[[Any], Optional[str]]
    ignore: frozenset = frozenset()
    labels: dict = field(default_factory=dict)
    # column → fn(label, old, new) -> [change]; JSON columns default to json_changes
    custom: dict = field(default_factory=dict)
    links: Callable[[Any], dict] = lambda o: {}
    rule: Optional[Callable[[Any], dict]] = None  # Rule History: {key, name, where, status, version}
    snapshot: frozenset = frozenset()             # keep the WHOLE previous value (mappings)


_ALWAYS_IGNORE = frozenset({"id", "created_at", "modified_at", "updated_at", "tenant_id"})


def _attr(o, *names):
    for n in names:
        v = getattr(o, n, None)
        if v not in (None, ""):
            return v
    return None


def _contract_labels() -> dict:
    try:
        import contract_types as ct
        return {f["attr"]: f["label"] for f in ct.FIELDS.values() if f.get("attr") and f.get("label")}
    except Exception:  # noqa: BLE001
        return {}


def _limit_words(path: tuple) -> str:
    """commercial_terms {key: {value, severity}} → the question the form asks."""
    try:
        import contract_types as ct
        spec = ct.AGREED_LIMITS.get(str(path[0])) or ct.TREATY_LIMITS.get(str(path[0])) or {}
    except Exception:  # noqa: BLE001
        spec = {}
    what = spec.get("label") or spec.get("question") or words(str(path[0]))
    rest = [str(x) for x in path[1:] if str(x) != "value"]
    if rest == ["severity"]:
        return f"{what} (how strict)"
    return " › ".join([what] + [words(x) for x in rest])


def _wording_words(path: tuple) -> str:
    if path and path[0] == "sections" and len(path) >= 2:
        tail = [words(str(x)) for x in path[2:] if str(x) not in ("body", "text")]
        return " › ".join([f"Section “{path[1]}”"] + tail)
    return " › ".join(words(str(x)) for x in path)


def _contract_custom() -> dict:
    return {
        "commercial_terms": lambda label, o, n: json_changes("Agreed terms", o, n, path_words=_limit_words),
        "wording_sections": lambda label, o, n: json_changes(
            "Wording", o, n, path_words=_wording_words, clip=CLIP_TEXT,
            skip=lambda p: bool(p) and p[0] in ("signature_layout", "dropped_sections", "dropped_terms")),
    }


def _template_fields(label, old, new) -> list[dict]:
    """An output template's columns, by sheet and name: what each field
    carries, whether it is on, required, its default and transformation."""
    try:
        import output_template_fields as otf
        flat = otf.flatten
    except Exception:  # noqa: BLE001
        return json_changes(label, old, new)
    props = (("source_field", "Source"), ("active", "Switched on"), ("required", "Required"),
             ("default_value", "Default value"), ("transformation_rule", "Transformation"),
             ("data_type", "Type"), ("display_name", "Heading"))

    def index(st):
        out = {}
        for f in flat(st or {}) if isinstance(st, dict) else []:
            out[(f.get("sheet") or "", f.get("field_key") or f.get("column_name") or f.get("display_name") or "")] = f
        return out

    a, b = index(old), index(new)
    changes = []
    for k in sorted(set(a) | set(b)):
        nm = (b.get(k) or a.get(k) or {}).get("display_name") or k[1]
        where = f"Field “{nm}”" + (f" ({k[0]})" if k[0] else "")
        if k not in a:
            changes.append({"field": where, "old": None, "new": "Added"})
            continue
        if k not in b:
            changes.append({"field": where, "old": "Removed", "new": None})
            continue
        for p, pw in props:
            o, n = show(a[k].get(p)), show(b[k].get(p))
            if p == "active":
                o, n = show(a[k].get(p, True)), show(b[k].get(p, True))
            if o != n:
                changes.append({"field": f"{where} › {pw}", "old": o, "new": n})
    return changes


def _mapping_changes(label, old, new):
    return json_changes(label, old, new)


def _rule_status(active) -> str:
    return "Active" if active in (True, 1) else "Switched off"


def _tracked() -> dict[type, Track]:
    """Built on first use, so importing this module never imports db."""
    import db
    out: dict[type, Track] = {
        db.Program: Track(
            "programme", "Programmes", "Programme", lambda o: o.name,
            ignore=frozenset({"canonical_status", "is_app_managed", "source_contract_file"}),
            links=lambda o: {"program_id": o.id}),
        db.Party: Track(
            "company", "Companies", "Company", lambda o: _attr(o, "legal_name", "dba_name"),
            ignore=frozenset({"is_app_managed", "verified_by_user_id"}),
            links=lambda o: {"broker_party_id": o.id}),
        db.PartyContact: Track(
            "contact", "Companies", "Company contact", lambda o: _attr(o, "full_name", "email"),
            links=lambda o: {"broker_party_id": o.party_id}),
        db.AppUser: Track(
            "user", "Users", "User", lambda o: _attr(o, "full_name", "email"),
            ignore=frozenset({"last_login_at", "notifications_seen_at", "accepted_at",
                              "reset_token", "reset_token_expires", "password", "invited_by_user_id"}),
            links=lambda o: {"broker_party_id": o.broker_party_id}),
        db.Tenant: Track(
            "carrier", "Carriers", "Carrier", lambda o: _attr(o, "legal_name", "tenant_name"),
            ignore=frozenset({"logo", "onboarding_skipped"})),
        db.Contract: Track(
            "contract", "Contracts", "Contract", lambda o: _attr(o, "name", "filename"),
            ignore=frozenset({"extracted", "blob_ref", "is_app_managed", "template_field_mappings",
                              "submitted_at"}),
            labels=_contract_labels(), custom=_contract_custom(),
            links=lambda o: {"program_id": o.program_id, "contract_id": o.id,
                             "broker_party_id": o.broker_party_id}),
        db.ProgramBroker: Track(
            "programme_broker", "Programmes", "Broker on a programme", lambda o: None,
            ignore=frozenset({"assigned_by_user_id", "created_by"}),
            links=lambda o: {"program_id": o.program_id, "broker_party_id": o.broker_party_id}),
        db.Pipeline: Track(
            "setup", "Bordereau Setup", "Bordereau Setup", lambda o: o.name,
            ignore=frozenset({"submitted_at"}),
            links=lambda o: {"program_id": o.program_id, "broker_party_id": o.broker_party_id}),
        db.DirectFormat: Track(
            "setup_mapping", "Bordereau Setup", "Bordereau Setup mapping", lambda o: o.name,
            ignore=frozenset({"fingerprint", "candidates", "mapping_decisions", "hit_count",
                              "datamodel_mapped", "datamodel_mapper_id"}),
            custom={"column_mapping": _mapping_changes, "sheet_routing": _mapping_changes},
            snapshot=frozenset({"column_mapping", "sheet_routing", "sheet_contracts"}),
            links=lambda o: {"program_id": o.program_id, "contract_id": o.contract_id}),
        db.Mapper: Track(
            "mapping", "Mappings", "Data mapping", lambda o: _attr(o, "name", "source_filename"),
            ignore=frozenset({"signature", "candidates", "samples", "output_by_source", "source_blob_ref",
                              "source_blob", "fingerprint_id", "source_filename"}),
            snapshot=frozenset({"spec", "spec_by_sheet"}),
            links=lambda o: {}),
        db.SheetBinding: Track(
            "sheet_binding", "Bordereau Setup", "Sheet setting", lambda o: o.sheet_name,
            ignore=frozenset({"reference_doc_ids"}),
            links=lambda o: {"program_id": o.program_id, "contract_id": o.contract_id}),
        db.ExportTemplate: Track(
            "output_template", "Output templates", "Output template",
            lambda o: (f"{o.name} (version {o.version})" if o.name and o.version else o.name),
            ignore=frozenset({"template_blob", "template_blob_ref", "standard_meta", "sample_comparison",
                              "carrier"}),
            custom={"structure": _template_fields},
            links=lambda o: {"program_id": o.program_id, "contract_id": o.contract_id,
                             "broker_party_id": o.broker_party_id},
            rule=lambda o: {"key": f"output_template:{o.tenant_id}:{o.name}", "name": o.name or "Output template",
                            "kind": "Output fields", "version": o.version,
                            "status": "In use" if o.is_active in (1, True) else "Not in use"}),
        db.GenericRuleSpecification: Track(
            "library_rule", "Rule Library", "Rule Library rule", lambda o: o.rule_name,
            ignore=frozenset({"created_by"}),
            labels={"validation_logic": "Check", "class_name": "Category", "is_active": "Switched on",
                    "is_generic": "Applies to every carrier"},
            rule=lambda o: {"key": f"library_rule:{o.id}", "name": o.rule_name or "Rule",
                            "kind": "Rule Library", "status": _rule_status(o.is_active)}),
    }
    try:
        import programme_rules as pr
        out[pr.ProgrammeRule] = Track(
            "programme_rule", "Programme Rules", "Programme rule", lambda o: o.name,
            ignore=frozenset({"created_by_user_id", "sent_at"}),
            labels={"operand": "Value", "operator": "Test", "field": "Field checked",
                    "broker_scope": "Brokers it covers", "treaty_scope": "Treaties it covers",
                    "approval_from": "Approval from"},
            links=lambda o: {"program_id": o.program_id},
            rule=lambda o: {"key": f"programme_rule:{o.id}", "name": o.name or "Programme rule",
                            "kind": "Programme rule", "version": o.version,
                            "status": {"draft": "Draft", "sent": "Active", "retired": "Retired"}
                            .get((o.status or "").lower(), (o.status or "").title())})
    except Exception as e:  # noqa: BLE001 — Programme Rules may not be set up on this database
        log.info("change_history: programme rules not tracked (%s)", e)
    return out


_TRACKED: Optional[dict] = None


def tracked() -> dict:
    global _TRACKED
    if _TRACKED is None:
        _TRACKED = _tracked()
    return _TRACKED


# ── names for ids, so a change reads "Broker: Acme → Zenith", not "12 → 15" ─
_ID_NAMES = {
    "broker_party_id": ("Party", ("legal_name",)), "party_id": ("Party", ("legal_name",)),
    "carrier_party_id": ("Party", ("legal_name",)), "program_id": ("Program", ("name",)),
    "contract_id": ("Contract", ("name", "filename")), "renews_contract_id": ("Contract", ("name", "filename")),
    "output_template_id": ("ExportTemplate", ("name",)),
}
_FLAGS = ("is_active", "approved")      # integer 0 / 1 columns that mean yes / no


def _id_name(s, col: str, v):
    if v is None:
        return None
    if col in _FLAGS and v in (0, 1) and not isinstance(v, bool):
        return bool(v)
    hit = _ID_NAMES.get(col)
    if not hit:
        if col.endswith("_user_id"):
            import db
            u = s.get(db.AppUser, v)
            return (u.full_name or u.email) if u else f"#{v}"
        return v
    import db
    try:
        o = s.get(getattr(db, hit[0]), v)
    except Exception:  # noqa: BLE001
        o = None
    return (_attr(o, *hit[1]) or f"#{v}") if o is not None else f"#{v}"


# ── one record's changes ───────────────────────────────────────────────────
def _changes(s, obj, t: Track) -> tuple[list[dict], dict]:
    """(field changes, whole previous values kept on file)."""
    st = sa_inspect(obj)
    changes: list[dict] = []
    before: dict = {}
    for attr in st.mapper.column_attrs:
        key = attr.key
        if key in _ALWAYS_IGNORE or key in t.ignore:
            continue
        h = st.attrs[key].history
        if not h.has_changes():
            continue
        # The value the code replaced — or, when it never loaded it, the one
        # _prefetch read from the database just before the flush wrote over it.
        old = h.deleted[0] if h.deleted else s.info.get(_OLD, {}).get((id(obj), key))
        new = h.added[0] if h.added else None
        label = t.labels.get(key) or words(key)
        if _SECRET.search(key):
            changes.append({"field": label, "old": "(hidden)", "new": "(changed)"})
            continue
        if key in t.snapshot and old is not None:
            before[key] = old
        if key in t.custom:
            changes.extend(t.custom[key](label, old, new))
        elif isinstance(old, (dict, list)) or isinstance(new, (dict, list)):
            changes.extend(json_changes(label, old, new))
        else:
            o, n = show(_id_name(s, key, old)), show(_id_name(s, key, new))
            if o != n:
                changes.append({"field": label, "old": o, "new": n})
    return changes, before


def _event_of(changes: list[dict], created: bool, deleted: bool) -> str:
    """What the save did, in one word: switching a rule off is not "a change"
    to whoever scans the list for it."""
    if created:
        return "created"
    if deleted:
        return "deleted"
    for c in changes:
        f, new, old = c["field"], (c["new"] or "").lower(), c["old"]
        if f in ("Switched on", "Is active", "Active", "In use"):
            if new == "no":
                return "switched_off"
            if new == "yes" and old:
                return "switched_on"
        if f == "Status":
            if new == "retired":
                return "retired"
            if new == "removed":
                return "removed"
            if new in ("disabled", "inactive"):
                return "switched_off"
    return "changed"


def _row(s, obj, t: Track, event_: str, changes: list[dict], before: dict, ctx: dict) -> dict:
    links = {}
    try:
        links = {k: v for k, v in (t.links(obj) or {}).items() if v is not None}
    except Exception:  # noqa: BLE001
        pass
    entity_id = getattr(obj, "id", None)
    name = None
    try:
        name = t.name(obj)
    except Exception:  # noqa: BLE001
        pass
    if t.entity == "programme_broker":
        name = _id_name(s, "broker_party_id", getattr(obj, "broker_party_id", None))
    details = {
        "entity": t.entity, "entity_id": entity_id, "name": show(name, 200),
        "module": t.module, "kind": t.kind_words, "event": event_,
        "changes": changes[:MAX_CHANGES], "more": max(0, len(changes) - MAX_CHANGES),
        "reason": ctx.get("reason"), **links,
    }
    if before:
        details["before"] = before
    if t.rule is not None:
        try:
            details["rule"] = {**t.rule(obj), "where": _rule_where(s, links)}
            if event_ == "deleted":
                details["rule"]["status"] = "Removed"
        except Exception:  # noqa: BLE001
            pass
    return {"tenant_id": getattr(obj, "tenant_id", None) or ctx.get("tenant_id"),
            "target": f"{t.entity}:{entity_id}", "details": details}


def _rule_where(s, links: dict) -> Optional[str]:
    if links.get("contract_id"):
        return show(_id_name(s, "contract_id", links["contract_id"]), 200)
    if links.get("program_id"):
        return show(_id_name(s, "program_id", links["program_id"]), 200)
    return None


# ── the hook ───────────────────────────────────────────────────────────────
_PENDING = "_kavachio_change_rows"
_OLD = "_kavachio_change_old"


def _prefetch(session, flush_context, instances):
    """before_flush: the stored value of every tracked field about to be
    written whose old value the code never loaded — an attribute expired by an
    earlier commit, or a deferred one like an output template's `structure`
    replaced without being read. SQLAlchemy keeps no old value for those, so
    without this the change would read "— → new" (or every template field
    "Added"). Read here, while the database still holds it: one SELECT per
    such record, only in a signed-in request. A value set to what it already
    was then shows no change, as it should."""
    ctx = _ctx.get()
    if not ctx or not ctx.get("user_id"):
        return
    try:
        from sqlalchemy import and_, select
        tr = tracked()
        found: dict = {}
        with session.no_autoflush:
            for obj in list(session.dirty):
                t = tr.get(type(obj))
                if t is None:
                    continue
                st = sa_inspect(obj)
                if st.identity is None:
                    continue
                missing = [a for a in st.mapper.column_attrs
                           if a.key not in _ALWAYS_IGNORE and a.key not in t.ignore and not _SECRET.search(a.key)
                           and st.attrs[a.key].history.added and not st.attrs[a.key].history.deleted]
                if not missing:
                    continue
                pk = st.mapper.primary_key
                row = session.execute(
                    select(*[a.columns[0] for a in missing])
                    .where(and_(*[c == v for c, v in zip(pk, st.identity)]))).first()
                if row is not None:
                    found.update({(id(obj), a.key): v for a, v in zip(missing, row)})
        if found:
            session.info.setdefault(_OLD, {}).update(found)
    except Exception as e:  # noqa: BLE001 — history must never break a save
        log.warning("change_history: could not read earlier values: %s", e)


def _collect(session, flush_context):
    try:
        _collect_rows(session)
    finally:
        session.info.pop(_OLD, None)   # this flush's only; ids are reused later


def _collect_rows(session):
    ctx = _ctx.get()
    if not ctx or not ctx.get("user_id"):
        return
    try:
        tr = tracked()
    except Exception as e:  # noqa: BLE001
        log.warning("change_history: tracking unavailable: %s", e)
        return
    rows = []
    try:
        with session.no_autoflush:
            for bucket, created, deleted in ((session.new, True, False), (session.dirty, False, False),
                                             (session.deleted, False, True)):
                for obj in list(bucket):
                    t = tr.get(type(obj))
                    if t is None:
                        continue
                    if created or deleted:
                        changes, before = [], {}
                        if created and t.entity == "output_template":
                            changes = _template_fork_changes(session, obj)
                    else:
                        if not session.is_modified(obj, include_collections=False):
                            continue
                        changes, before = _changes(session, obj, t)
                        if not changes:
                            continue
                    rows.append(_row(session, obj, t, _event_of(changes, created, deleted),
                                     changes, before, ctx))
    except Exception as e:  # noqa: BLE001 — history must never break a save
        log.warning("change_history: could not read changes: %s", e)
        return
    if rows:
        session.info.setdefault(_PENDING, []).extend(rows)


def _template_fork_changes(s, obj) -> list[dict]:
    """A new output template version: what it changed against the one before."""
    import db
    if not obj.name or not obj.version or obj.version <= 1:
        return []
    prev = (s.query(db.ExportTemplate)
            .filter(db.ExportTemplate.tenant_id == obj.tenant_id, db.ExportTemplate.name == obj.name,
                    db.ExportTemplate.version < obj.version)
            .order_by(db.ExportTemplate.version.desc()).first())
    if prev is None:
        return []
    return _template_fields("Fields", prev.structure, obj.structure)


def _write(session, _flush_context=None):
    rows = session.info.pop(_PENDING, None)
    if not rows:
        return
    ctx = _ctx.get() or {}
    try:
        import audit
        import db
        uid = ctx.get("user_id")
        email, role, broker_id = audit.actor_identity(uid)
        for r in rows:
            session.add(db.ActivityEvent(
                tenant_id=r["tenant_id"], actor=email, action=ACTION, target=r["target"],
                details=r["details"], actor_user_id=uid, actor_role=role,
                actor_broker_party_id=broker_id))
    except Exception as e:  # noqa: BLE001
        log.warning("change_history: could not record %s change(s): %s", len(rows), e)


def _discard(session, *_a):
    session.info.pop(_PENDING, None)
    session.info.pop(_OLD, None)


_installed = False


def install() -> None:
    """Register the hooks once (main.py)."""
    global _installed
    if _installed:
        return
    event.listen(Session, "before_flush", _prefetch)
    event.listen(Session, "after_flush", _collect)
    event.listen(Session, "after_flush_postexec", _write)
    event.listen(Session, "after_rollback", _discard)
    import rule_history
    rule_history.install()
    _installed = True


# ═══════════════════════════════════════════════════════════════════════════
# READ SIDE — the Change History tab
# ═══════════════════════════════════════════════════════════════════════════
MAX_ROWS = 5000
EVENT_WORDS = {"created": "Created", "changed": "Changed", "deleted": "Deleted",
               "switched_off": "Switched off", "switched_on": "Switched on",
               "removed": "Removed", "retired": "Retired"}
MODULES = ("Programmes", "Companies", "Users", "Carriers", "Contracts", "Bordereau Setup", "Mappings",
           "Output templates", "Rule Library", "Programme Rules", "Contract rules")


def _query(s, principal, f, *, module=None, entity=None, rules_only=False):
    """activity_events "record_changed" rows this viewer may read, newest first."""
    from sqlalchemy import cast, desc
    from sqlalchemy.dialects.postgresql import JSONB
    import audit_feed as af
    from db import ActivityEvent
    v = af.viewer_for(s, principal)
    sc = af.scope_for(s, v)
    q = af._base_query(s, ActivityEvent, sc, f, af._search_targets(s, f.q) if f.q else ([], []))
    if q is None:
        return v, None
    q = q.filter(ActivityEvent.action == ACTION)
    d = cast(ActivityEvent.details, JSONB)
    if module:
        q = q.filter(d["module"].astext == module)
    if entity:
        q = q.filter(ActivityEvent.target == entity)
    if rules_only:
        q = q.filter(d.has_key("rule"))   # JSONB's "?" operator
    return v, q.order_by(desc(ActivityEvent.created_at), desc(ActivityEvent.id))


def _iso(x) -> Optional[str]:
    return (x.isoformat() + "Z") if isinstance(x, dt.datetime) else None


def _base(e, people) -> dict:
    d = e.details if isinstance(e.details, dict) else {}
    return {"event_id": e.id, "at": _iso(e.created_at), "entity": d.get("entity"),
            "target": e.target, "module": d.get("module"), "kind": d.get("kind"),
            "name": d.get("name") or "—", "event": EVENT_WORDS.get(d.get("event"), d.get("event")),
            "reason": d.get("reason"), "by": people.name(e.actor_user_id, "Kavachio · Automatic")}


def changes(s, principal, f, *, module=None, entity=None, page=1, page_size=50) -> dict:
    """One row per field changed, newest first — a save of three fields is
    three rows, as the before / after is per field."""
    import audit_history as ah
    v, q = _query(s, principal, f, module=module, entity=entity)
    if q is None:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size,
                "counts": {"changes": 0, "records": 0, "people": 0, "with_reason": 0}}
    people = ah.People(s, v)
    items, records, who, saves, reasoned = [], set(), set(), 0, 0
    for e in q.limit(MAX_ROWS):
        d = e.details if isinstance(e.details, dict) else {}
        base = _base(e, people)
        records.add(e.target)
        who.add(e.actor_user_id)
        saves += 1
        reasoned += bool(d.get("reason"))
        lines = d.get("changes") or []
        if not lines:
            items.append({**base, "field": None, "old": None, "new": None, "count": 0})
            continue
        for n, c in enumerate(lines):
            items.append({**base, "field": c.get("field"), "old": c.get("old"), "new": c.get("new"),
                          "line": n, "count": len(lines) + int(d.get("more") or 0)})
    out = ah._page(items, page, page_size)
    out["counts"] = {"changes": len(items), "records": len(records), "people": len({w for w in who if w}),
                     "saves": saves, "with_reason": reasoned}
    return out


def change(s, principal, event_id: int) -> dict:
    """One save in full: every field before and after, the reason, and the
    record's other saves (newest first) for "View full history"."""
    from fastapi import HTTPException
    import audit_history as ah
    from db import ActivityEvent
    import audit_feed as af
    v, q = _query(s, principal, af.Filters())
    e = q.filter(ActivityEvent.id == event_id).first() if q is not None else None
    if e is None:
        raise HTTPException(404, "That change is not one you can see.")
    people = ah.People(s, v)
    d = e.details if isinstance(e.details, dict) else {}
    others = [_base(x, people) for x in q.filter(ActivityEvent.target == e.target).limit(50)]
    return {**_base(e, people), "changes": d.get("changes") or [], "more": int(d.get("more") or 0),
            "before": d.get("before"), "rule": d.get("rule"),
            "history": [{k: o[k] for k in ("event_id", "at", "event", "by", "reason")} for o in others]}


def edits_near(s, contract_ids: list[int]) -> dict[int, list[tuple[dt.datetime, Optional[int], int]]]:
    """Task 49.3: the recorded contract saves, by contract — (when, who, row id),
    so Contract History can open a "Draft changed" step's before / after."""
    from db import ActivityEvent
    if not contract_ids:
        return {}
    out: dict[int, list] = {}
    rows = (s.query(ActivityEvent.id, ActivityEvent.target, ActivityEvent.created_at, ActivityEvent.actor_user_id)
            .filter(ActivityEvent.action == ACTION,
                    ActivityEvent.target.in_([f"contract:{i}" for i in contract_ids])).all())
    for rid, target, at, uid in rows:
        out.setdefault(int(target.split(":")[1]), []).append((at, uid, rid))
    return out


def match_edit(edits: list, at: Optional[dt.datetime], user_id: Optional[int]) -> Optional[int]:
    """The recorded save behind one "Draft changed" step: same person, within
    a minute (the request's own audit row is written just after the save)."""
    if at is None:
        return None
    best = None
    for when, uid, rid in edits or []:
        if user_id and uid and uid != user_id:
            continue
        gap = abs((when - at).total_seconds()) if when else 1e9
        if gap <= 60 and (best is None or gap < best[0]):
            best = (gap, rid)
    return best[1] if best else None


CSV_COLUMNS = (("at", "Date and time (UTC)"), ("module", "Module"), ("kind", "What"), ("name", "Record"),
               ("event", "What happened"), ("field", "Field changed"), ("old", "Old value"),
               ("new", "New value"), ("by", "Changed by"), ("reason", "Reason"))
