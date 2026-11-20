"""
treaty_history.py
─────────────────
Audit Logs › Contract History › Reinsurance treaties: every step on each reinsurance treaty — added,
structured, sent to the reinsurers, opened, approved, signed, its terms changed
from a date, endorsed, attached to programmes, made active, ended, renewed —
from the insurer's side and each reinsurer's. Contract History
(audit_history.contracts) leaves treaties out: a treaty's other side is a
reinsurer, not a broker (contract_types: counterparty_party_type).

READ ONLY, from what is already kept; no table of its own, no migration, and
nothing in treaty_*.py / ceded_*.py changed:
  contract                    the treaty itself (added, status, dates, renewal)
  contract_approval           its approval steps (audit_history.STEPS words)
  reinsurance_treaty(+layer, share, programme)   structure, reinsurers, shares, programmes
  reinsurer_access_link       sent to, first opened, sign asked, withdrawn
  reinsurer_approval          each reinsurer's answer, with its note
  treaty_party_signature      each reinsurer's signature (and withdrawal)
  treaty_term                 each dated change to the terms (and its removal)
  activity_events             everything else named treaty_* / contract_*
A step that has its own table is read from the table (it says more), and its
twin in the activity log is skipped (_FROM_TABLES).

WHO: the carrier its own treaties, Kavachio every carrier's (or one, ?mga=).
A broker never — a treaty is between the insurer and its reinsurers.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import or_

import audit_history as ah

TREATY_TYPE = "insurer_reinsurer"

# Log actions → (words, kind, status after). The reinsurer's name goes in {r}.
WORDS = {
    "treaty_structure_saved": ("Structure changed", "Structure", None),
    "signed_treaty_uploaded": ("Signed treaty uploaded", "Draft", None),
    "treaty_wording_read": ("Signed wording read", "Wording", None),
    "treaty_clauses_read": ("Clauses read from the wording", "Wording", None),
    "treaty_terms_confirmed": ("Terms confirmed from the wording", "Wording", None),
    "treaty_sent_for_approval": ("Sent to the reinsurers for approval", "Approval", None),
    "treaty_endorsement_read": ("Endorsement read", "Endorsement", None),
    "treaty_endorsement_applied": ("Endorsement applied", "Endorsement", None),
    "treaty_endorsement_sent_for_approval": ("Endorsement sent to the reinsurers", "Endorsement", None),
    "treaty_endorsement_approved_by_reinsurer": ("Endorsement approved by {r}", "Endorsement", None),
    "treaty_endorsement_changes_requested_by_reinsurer": ("{r} asked for changes to an endorsement", "Endorsement", None),
    "treaty_endorsement_signed": ("Endorsement signed by the insurer", "Endorsement", None),
    "treaty_endorsement_signed_by_reinsurer": ("Endorsement signed by {r}", "Endorsement", None),
    "treaty_basis_sent_for_approval": ("Term changes sent to the reinsurers", "Terms", None),
    "treaty_basis_approved_by_reinsurer": ("Term changes approved by {r}", "Terms", None),
    "treaty_basis_changes_requested_by_reinsurer": ("{r} sent back term changes", "Terms", None),
    "treaty_programmes_updated": ("Programmes it protects changed", "Programmes", None),
    "treaty_detached_from_programme": ("No longer protects {p}", "Programmes", None),
    "treaty_link_resent": ("Link sent again", "Link", None),
}
# Their own tables say more; their log twins are skipped.
_FROM_TABLES = {"treaty_term_changed", "treaty_term_change_removed", "treaty_approved_by_reinsurer",
                "treaty_changes_requested_by_reinsurer", "treaty_signed_by_reinsurer",
                "treaty_shared_with_reinsurer", "treaty_link_revoked", "treaty_attached_to_programme"}
_TARGETS = (re.compile(r"^contract:(\d+)$"), re.compile(r"^/contracts/(\d+)(?:/|$)"),
            re.compile(r"^/programs/(\d+)/treaties/(\d+)$"))
SIGN_METHOD = {"link": "on its link", "round": "in the signing round", "paper": "on paper"}
AUTOMATIC = "Kavachio · Automatic"


def _name(c) -> str:
    return c.name or c.filename or f"Treaty #{c.id}"


def _reach(s, principal, mga=None) -> ah.Reach:
    r = ah.reach(s, principal, mga)
    if r.v.is_broker:
        raise HTTPException(403, "Treaty History is for carriers.")
    return r


def _treaties(s, r: ah.Reach, *, treaty=None, programme=None, reinsurer=None) -> list:
    from db import Contract, TreatyProgramme, TreatyShare
    q = s.query(Contract).filter(Contract.contract_type == TREATY_TYPE)
    if r.tenants is not None:
        q = q.filter(Contract.tenant_id.in_(sorted(r.tenants) or [0]))
    if treaty:
        q = q.filter(Contract.id == treaty)
    if programme:
        q = q.filter(or_(Contract.program_id == programme, Contract.id.in_(
            s.query(TreatyProgramme.contract_id).filter(TreatyProgramme.program_id == programme))))
    if reinsurer:
        q = q.filter(or_(Contract.broker_party_id == reinsurer, Contract.id.in_(
            s.query(TreatyShare.contract_id).filter(TreatyShare.reinsurer_party_id == reinsurer))))
    return q.all()


class _Facts:
    """Who is on each treaty, and on which programmes — read once for many."""

    def __init__(self, s, treaties: list):
        from db import Party, Program, Treaty, TreatyLayer, TreatyProgramme, TreatyShare
        ids = [c.id for c in treaties] or [0]
        self.structure = {t.contract_id: t for t in s.query(Treaty).filter(Treaty.contract_id.in_(ids))}
        self.layers: dict[int, list] = {}
        for x in s.query(TreatyLayer).filter(TreatyLayer.contract_id.in_(ids)).order_by(TreatyLayer.position):
            self.layers.setdefault(x.contract_id, []).append(x)
        self.shares: dict[int, list] = {}
        for x in s.query(TreatyShare).filter(TreatyShare.contract_id.in_(ids)):
            self.shares.setdefault(x.contract_id, []).append(x)
        self.programmes: dict[int, list] = {c.id: ([c.program_id] if c.program_id else []) for c in treaties}
        self.attached: list = s.query(TreatyProgramme).filter(TreatyProgramme.contract_id.in_(ids)).all()
        for x in self.attached:
            if x.program_id not in self.programmes.setdefault(x.contract_id, []):
                self.programmes[x.contract_id].append(x.program_id)
        parties = {c.broker_party_id for c in treaties} | {x.reinsurer_party_id for v in self.shares.values() for x in v}
        self.party = dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_({p for p in parties if p} or {0})))
        progs = {p for v in self.programmes.values() for p in v}
        self.programme = dict(s.query(Program.id, Program.name).filter(Program.id.in_(progs or {0})))

    def reinsurers(self, c) -> list[int]:
        out = [x.reinsurer_party_id for x in self.shares.get(c.id, [])]
        if c.broker_party_id and c.broker_party_id not in out:
            out.insert(0, c.broker_party_id)
        return list(dict.fromkeys(out))

    def reinsurer_names(self, c) -> str:
        return ", ".join(self.party.get(p) or f"#{p}" for p in self.reinsurers(c)) or "—"

    def programme_names(self, c) -> str:
        return ", ".join(self.programme.get(p) or f"#{p}" for p in self.programmes.get(c.id, [])) or "—"


def _term_words(s, c, facts: _Facts, rows: list) -> dict[int, tuple[str, str]]:
    """Each dated term change as "Cession changed to 40% from 1 Jan 2027"."""
    import treaty_basis as tb
    st = facts.structure.get(c.id)
    specs = {x["term"]: x for x in tb.spec(st.treaty_type if st else None)}
    cur = (((st.terms or {}) if st else {}).get("currency") or "").upper() or None
    lays = facts.layers.get(c.id, [])
    out = {}
    for r in rows:
        sp = specs.get(r.term) or {"label": r.term.replace("_", " ").capitalize(), "kind": ""}
        layer = lays[r.layer_index].name if r.layer_index is not None and r.layer_index < len(lays) else None
        try:
            value = tb._show(sp["kind"], r.value, cur) or str(r.value)
        except Exception:  # noqa: BLE001 — a value we cannot word is still shown
            value = str(r.value)
        label = sp["label"] + (f" ({layer})" if layer else "")
        out[r.id] = (label, f"{label} changed to {value} from {r.effective_from:%d %b %Y}")
    return out


def _steps_of(s, treaties: list, facts: _Facts, people: ah.People) -> dict[int, list[dict]]:
    """Every step of every treaty given, oldest first, with the status before and after."""
    from db import (ActivityEvent, ContractApproval, ReinsurerAccessLink, ReinsurerApproval,
                    TreatyPartySignature, TreatyTerm)
    ids = [c.id for c in treaties]
    if not ids:
        return {}
    by_id = {c.id: c for c in treaties}
    raw: dict[int, list[dict]] = {c.id: [] for c in treaties}

    def add(cid, at, action, title, kind, by, note=None, after=None, side="insurer", party=None):
        if at is None or cid not in raw:
            return
        raw[cid].append({"_k": ah._when(at), "action": action, "title": title, "kind": kind, "by": by,
                         "note": note, "after": after, "side": side, "party": party})

    def rname(pid) -> str:
        return facts.party.get(pid) or "A reinsurer"

    for c in treaties:
        add(c.id, c.created_at, "raised", "Treaty added", "Draft", people.name(c.submitted_by_user_id, "—"))
        st = facts.structure.get(c.id)
        if st is not None:
            import treaty_types as tt
            n_l, n_r = len(facts.layers.get(c.id, [])), len(facts.reinsurers(c))
            add(c.id, st.created_at, "structure", f"Structure set: {tt.label(st.treaty_type) or st.treaty_type}, "
                f"{n_l} layer{'' if n_l == 1 else 's'}, {n_r} reinsurer{'' if n_r == 1 else 's'}", "Structure",
                people.name(st.updated_by_user_id, "—"))
    for a in s.query(ContractApproval).filter(ContractApproval.contract_id.in_(ids)):
        words, kind, after = ah.STEPS.get(a.action, (ah._status_words(a.action), "Change", None))
        words = words.replace("the broker", "the reinsurers").replace("Broker asked", "A reinsurer asked")
        add(a.contract_id, a.acted_at, a.action, words, kind,
            people.name(a.acted_by_user_id, AUTOMATIC), a.note, after)
    for x in facts.attached:
        c = by_id.get(x.contract_id)
        if c is not None and x.program_id != c.program_id:
            add(x.contract_id, x.created_at, "programme", f"Now protects {facts.programme.get(x.program_id) or 'a programme'}",
                "Programmes", "—")
    for l in s.query(ReinsurerAccessLink).filter(ReinsurerAccessLink.contract_id.in_(ids)):
        r = rname(l.party_id)
        add(l.contract_id, l.sent_at or l.created_at, "link_sent", f"Treaty sent to {r} ({l.name})", "Link",
            people.name(l.sent_by_user_id, "—"))
        add(l.contract_id, l.first_opened_at, "link_opened", f"{r} opened the treaty", "Link", r, side="reinsurer")
        add(l.contract_id, l.sign_requested_at, "sign_asked", f"{r} asked to sign", "Signing",
            people.name(l.sign_requested_by_user_id, "—"))
        add(l.contract_id, l.revoked_at, "link_revoked", f"Link to {r} withdrawn", "Link",
            people.name(l.revoked_by_user_id, "—"))
    for a in s.query(ReinsurerApproval).filter(ReinsurerApproval.contract_id.in_(ids)):
        r = rname(a.party_id)
        who = f"{a.signer_name} · {r}" if a.signer_name else r
        if a.status == "approved":
            add(a.contract_id, a.at, "reinsurer_approved", f"Approved by {r}", "Approval", who, a.note,
                side="reinsurer", party=a.party_id)
        else:
            add(a.contract_id, a.at, "reinsurer_changes", f"{r} asked for changes", "Approval", who, a.note,
                side="reinsurer", party=a.party_id)
    for g in s.query(TreatyPartySignature).filter(TreatyPartySignature.contract_id.in_(ids)):
        r = rname(g.party_id)
        signer = ", ".join(x for x in (g.signer_name, g.signer_title) if x)
        by = f"{signer} · {r}" if g.method != "paper" else people.name(g.recorded_by_user_id, "—")
        add(g.contract_id, g.signed_at, "reinsurer_signed",
            f"Signed by {r} ({SIGN_METHOD.get(g.method, g.method)})", "Signing",
            by, g.note, side="reinsurer" if g.method != "paper" else "insurer", party=g.party_id)
        add(g.contract_id, g.withdrawn_at, "signature_withdrawn", f"Signature of {r} withdrawn", "Signing",
            people.name(g.withdrawn_by_user_id, "—"), party=g.party_id)
    terms = s.query(TreatyTerm).filter(TreatyTerm.contract_id.in_(ids), TreatyTerm.source != "structure").all()
    by_treaty: dict[int, list] = {}
    for t in terms:
        by_treaty.setdefault(t.contract_id, []).append(t)
    for cid, rows in by_treaty.items():
        words = _term_words(s, by_id[cid], facts, rows)
        for t in rows:
            label, said = words[t.id]
            src = {"endorsement": "Endorsement", "wording": "Terms"}.get(t.source, "Terms")
            add(cid, t.created_at, "term_changed", said, src, t.created_by_name or people.name(t.created_by_user_id, "—"),
                t.note)
            add(cid, t.removed_at, "term_removed", f"Dated change to {label} removed", "Terms",
                people.name(t.removed_by_user_id, "—"))
    # Everything else, from the activity log.
    tenants = {c.tenant_id for c in treaties if c.tenant_id}
    actions = [a for a in WORDS] + [a for a in ah._FROM_LOG]
    q = s.query(ActivityEvent).filter(ActivityEvent.action.in_(actions))
    if tenants:
        q = q.filter(or_(ActivityEvent.tenant_id.in_(sorted(tenants)), ActivityEvent.tenant_id.is_(None)))
    for ev in q:
        cid = _treaty_of(ev.target, ev.details)
        if cid not in raw:
            continue
        d = ev.details if isinstance(ev.details, dict) else {}
        if ev.action == "treaty_structure_saved":
            st = facts.structure.get(cid)
            if st is not None and abs((ah._when(ev.created_at) - ah._when(st.created_at)).total_seconds()) < 120:
                continue        # the first save is "Structure set", read from the treaty itself
        if ev.action in WORDS:
            words, kind, after = WORDS[ev.action]
            prog = None
            m = _TARGETS[2].match(ev.target or "")
            if m:
                prog = facts.programme.get(int(m.group(1)))
            title = words.format(r=d.get("reinsurer") or "a reinsurer", p=prog or "a programme")
        else:
            words, kind, after = ah.STEPS[ev.action]
            title = words
        rside = "by_reinsurer" in ev.action or "requested_by_reinsurer" in ev.action
        by = (f"{d.get('signer_name')} · {d.get('reinsurer')}" if rside and d.get("signer_name")
              else d.get("reinsurer") if rside else people.name(ev.actor_user_id, AUTOMATIC))
        add(cid, ev.created_at, ev.action, title, kind, by, d.get("note") or d.get("reason"), after,
            side="reinsurer" if rside else "insurer")
    out: dict[int, list[dict]] = {}
    for cid, steps in raw.items():
        steps.sort(key=lambda x: x["_k"])
        _statuses(by_id[cid], steps, len(facts.reinsurers(by_id[cid])))
        now = None
        rows = []
        for st in steps:
            before = now
            if st["after"]:
                now = st["after"]
            rows.append({**st, "at": ah._iso(st["_k"]) if st["_k"] != dt.datetime.min else None,
                         "before": before if st["after"] else None})
        out[cid] = rows
    return out


# Steps that move a treaty towards the reinsurers, and those that come back.
_SENT = {"treaty_sent_for_approval", "link_sent"}
_ASKED = {"reinsurer_changes", "treaty_changes_requested_by_reinsurer"}
_REINSURER_STEPS = _SENT | _ASKED | {"reinsurer_approved", "reinsurer_signed", "sign_asked"}


def _statuses(c, steps: list[dict], n_reinsurers: int) -> None:
    """The treaty's status after each step, where the step moves it — so the
    history reads Draft → Waiting for reinsurers → Approved by 1 of 2 →
    Approved by all reinsurers → Signed…, not "No change" all the way down.
    A step that already says its status (ContractApproval's) keeps it."""
    n = max(n_reinsurers, 1)
    approved: set = set()
    signed: set = set()
    goes_out = any(st["action"] in _REINSURER_STEPS for st in steps)
    for st in steps:
        if st["after"]:
            continue
        a = st["action"]
        if a == "raised":
            # Uploaded already signed: active from the start (app_routes sets
            # it so at upload) — unless it then went to the reinsurers.
            st["after"] = "Active" if (c.lifecycle or "").lower() == "active" and not goes_out else "Draft"
        elif a == "signed_treaty_uploaded":
            st["after"] = "Signed"
        elif a in _SENT:
            if not approved and not signed:
                st["after"] = "Waiting for reinsurers"
        elif a in _ASKED:
            approved.discard(st.get("party"))
            st["after"] = "Changes requested"
        elif a == "reinsurer_approved":
            approved.add(st.get("party"))
            k = len(approved)
            st["after"] = "Approved by all reinsurers" if k >= n else f"Approved by {k} of {n}"
        elif a == "reinsurer_signed":
            signed.add(st.get("party"))
            k = len(signed)
            st["after"] = "Signed by all reinsurers" if k >= n else f"Signed by {k} of {n}"
        elif a == "signature_withdrawn":
            signed.discard(st.get("party"))
            k = len(signed)
            st["after"] = f"Signed by {k} of {n}" if k else "Waiting for signatures"


def _treaty_of(target: Optional[str], details) -> Optional[int]:
    d = details if isinstance(details, dict) else {}
    if isinstance(d.get("contract_id"), int):
        return d["contract_id"]
    t = target or ""
    for i, rx in enumerate(_TARGETS):
        m = rx.match(t)
        if m:
            return int(m.group(2 if i == 2 else 1))
    return None


def _standing(s, c, facts: _Facts) -> list[dict]:
    """Each reinsurer on the treaty: its share, lead, and where it stands."""
    from db import ReinsurerApproval, TreatyPartySignature
    answers: dict[int, str] = {}
    for a in (s.query(ReinsurerApproval).filter(ReinsurerApproval.contract_id == c.id)
              .order_by(ReinsurerApproval.at.asc())):
        answers[a.party_id] = a.status
    signed = {g.party_id for g in s.query(TreatyPartySignature).filter(
        TreatyPartySignature.contract_id == c.id, TreatyPartySignature.withdrawn_at.is_(None))}
    shares: dict[int, dict] = {}
    for x in facts.shares.get(c.id, []):
        row = shares.setdefault(x.reinsurer_party_id, {"pct": 0.0, "lead": False})
        row["pct"] += float(x.share_pct or 0)
        row["lead"] = row["lead"] or bool(x.is_lead)
    out = []
    for p in facts.reinsurers(c):
        st = ("Signed" if p in signed else "Approved" if answers.get(p) == "approved"
              else "Asked for changes" if answers.get(p) == "changes_requested" else "Waiting")
        sh = shares.get(p)
        out.append({"id": p, "name": facts.party.get(p) or f"#{p}",
                    "share": f"{sh['pct']:g}%" if sh else None, "lead": bool(sh and sh["lead"]), "standing": st})
    return out


def _active(c) -> bool:
    return (c.lifecycle or c.status or "").lower() in ("active", "in_force", "bound")


def history(s, principal, *, mga=None, reinsurer=None, programme=None, treaty=None, status=None,
            since=None, until=None, page: int = 1, page_size: int = 50) -> dict:
    """Every step on the treaties picked, in the dates picked, newest first."""
    r = _reach(s, principal, mga)
    every = _treaties(s, r)
    rows = _treaties(s, r, treaty=treaty, programme=programme, reinsurer=reinsurer)
    if status:
        rows = [c for c in rows if ah._status_words(c.lifecycle or c.status) == status]
    facts = _Facts(s, rows)
    steps = _steps_of(s, rows, facts, ah.People(s, r.v))
    a, b = ah._day(since), ah._day(until)
    lo = dt.datetime.combine(a, dt.time()) if a else None
    hi = dt.datetime.combine(b, dt.time()) + dt.timedelta(days=1) if b else None
    by_id = {c.id: c for c in rows}
    items = []
    for cid, ss in steps.items():
        c = by_id[cid]
        for st in ss:
            k = st["_k"]
            if (lo and k < lo) or (hi and k >= hi):
                continue
            items.append({**{x: st[x] for x in st if x != "_k"}, "_k": k, "treaty_id": cid, "treaty": _name(c),
                          "reinsurers": facts.reinsurer_names(c), "programme": facts.programme_names(c)})
    items.sort(key=lambda x: x.pop("_k"), reverse=True)
    changed = {cid for cid, ss in steps.items() if any(x["action"] in ("term_changed", "treaty_endorsement_applied")
                                                         for x in ss)}
    waiting = sum(1 for c in rows if any(x["standing"] in ("Waiting", "Asked for changes")
                                         for x in _standing(s, c, facts)))
    out = ah._page(items, page, page_size)
    out["counts"] = {"treaties": len(rows), "active": sum(1 for c in rows if _active(c)),
                     "terms_changed": len(changed), "waiting": waiting}
    all_facts = _Facts(s, every)
    out["choices"] = {
        "treaties": sorted(({"id": c.id, "name": _name(c)} for c in every), key=lambda x: x["name"].lower()),
        "reinsurers": sorted(({"id": p, "name": n} for p, n in all_facts.party.items()), key=lambda x: (x["name"] or "").lower()),
        "programmes": sorted(({"id": p, "name": n} for p, n in all_facts.programme.items()), key=lambda x: (x["name"] or "").lower()),
        "statuses": sorted({ah._status_words(c.lifecycle or c.status) for c in every}),
    }
    return out


def timeline(s, principal, treaty_id: int, mga=None) -> dict:
    from db import Contract
    import treaty_types as tt
    r = _reach(s, principal, mga)
    c = s.get(Contract, treaty_id)
    if c is None or c.contract_type != TREATY_TYPE or (r.tenants is not None and c.tenant_id not in r.tenants):
        raise HTTPException(404, "That treaty is not one of yours.")
    facts = _Facts(s, [c])
    steps = _steps_of(s, [c], facts, ah.People(s, r.v))[c.id]
    for st in steps:
        st.pop("_k", None)
    st = facts.structure.get(c.id)
    renews = s.get(Contract, c.renews_contract_id) if c.renews_contract_id else None
    renewed_by = s.query(Contract).filter(Contract.renews_contract_id == c.id).first()
    return {"id": c.id, "name": _name(c), "type": (tt.label(st.treaty_type) if st else None),
            "status": ah._status_words(c.lifecycle or c.status),
            "starts": ah._iso(c.inception_dt), "ends": ah._iso(c.expiry_dt),
            "programmes": facts.programme_names(c), "reinsurers": _standing(s, c, facts),
            "renews": renews and {"id": renews.id, "name": _name(renews)},
            "renewed_by": renewed_by and {"id": renewed_by.id, "name": _name(renewed_by)},
            "steps": steps}
