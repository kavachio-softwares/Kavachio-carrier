"""Audit Logs as ONE list (20 Nov 2026) — the filters that replaced the tabs.

The trail itself is audit_feed's: the same stores, the same scope (scope_for),
the same words. This module adds what the old tabs used to answer:

  Area       which part of Kavachio (Broker contracts, Reinsurance, Contract checks…)
  Action     what kind of step (Added, Changed, Approved, Signed…)
  Programme  rows about one programme
  Status     the badge's own words (Signed, Activated, Deleted…), per Area
  Old / New  the before and after of a save — Change History's and Rule
             History's record_changed rows, attached to the trail row of the
             same save instead of being listed a second time.

Area and Action narrow the SQL (an action-name list, like the old Action type
filter). Programme and Status depend on the finished row, so when either is
chosen the newest MAX_FILTERED rows are read, finished and filtered here.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Optional

import audit_feed as af

# ── what is known by name ───────────────────────────────────────────────────
_GROUPS = {k: (acts, seats) for k, _lbl, acts, seats in af.ACTION_GROUPS}


def _known() -> set[str]:
    import audit
    names = {k for k in af.ACTION_WORDS if isinstance(k, str)}
    names |= {a for _k, (acts, _s) in _GROUPS.items() for a in acts}
    names |= {v for v in audit._FRIENDLY.values() if isinstance(v, str)}
    names.add("exception_decided")
    return {n for n in names if "/" not in n and " " not in n}


KNOWN = _known()

_RULE_MARKS = ("rule_", "rule.", "rules_", "clause.", "checks_", "margin_changed", "tolerance")
_CONTRACTS = _GROUPS["contracts"][0]
_CONTRACT_CHECKS = tuple(a for a in _CONTRACTS if any(m in a for m in _RULE_MARKS))
# The Rule Library screen writes rule_created / _updated / _deleted / _enabled /
# _disabled (app_routes /rule-library); the checks on a contract use the dotted
# names and the contract.* ones.
_LIBRARY = ("rule_created", "rule_updated", "rule_deleted", "rule_enabled", "rule_disabled", "rule_toggled",
            "rule_library_updated", "rule_library_checks_updated")
_RULES = tuple(a for a in _CONTRACT_CHECKS if a not in _LIBRARY)
_REINS_SIGNED = ("treaty_signed_by_reinsurer", "treaty_endorsement_signed",
                 "treaty_endorsement_signed_by_reinsurer", "signed_treaty_uploaded")
_TREATY = tuple(sorted(n for n in KNOWN if n.startswith("treaty_") and n not in _REINS_SIGNED))
_REINS_SETUP = tuple(sorted(n for n in KNOWN if n.startswith("reinsurance_setup")))
_REINS_BDX = tuple(sorted(n for n in KNOWN if n.startswith("reinsurance_") and n not in _REINS_SETUP))
_REPORTS = tuple(a for a in _GROUPS["brokers"][0] if "report" in a)
_DEADLINES = tuple(a for a in _GROUPS["brokers"][0] if a.startswith("submission_")) + af.REMINDER_ACTIONS
_POLICIES = ("policies_exported", "policies_sent")
_ALL = af.ALL_SEATS
_CARRIER = af.CARRIER_SEATS
_PARTY = af.CARRIER_SEATS + ("broker_admin",)

# Sub-areas: (key, words on screen, action names, seats offered it). The keys of
# the areas that came before are kept, so old links (?action=contracts) work.
AREAS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("contracts", "Broker contracts", tuple(a for a in _CONTRACTS if a not in _CONTRACT_CHECKS), _PARTY),
    ("treaties", "Reinsurance contracts", _TREATY, _CARRIER),
    ("signing", "Contract signing", _GROUPS["signing"][0] + _REINS_SIGNED, _PARTY),
    ("rules", "Contract checks", _RULES, _PARTY),
    ("reinsurance_setup", "Reinsurance BDX setup", _REINS_SETUP, _CARRIER),
    ("reinsurance_bdx", "Process Reinsurance BDX", _REINS_BDX, _CARRIER),
    ("files", "File submissions", tuple(a for a in _GROUPS["files"][0] if a != "contract_uploaded"), _ALL),
    ("exceptions", "Errors and fixes", _GROUPS["exceptions"][0], _ALL),
    ("calendar", "Bordereau calendar", _DEADLINES, _ALL),
    ("policies", "Policy Explorer", _POLICIES, _CARRIER),
    ("intake", "File sending channels", _GROUPS["intake"][0] + ("file_arrival_released",), _CARRIER),
    ("brokers", "Programmes and brokers",
     tuple(a for a in _GROUPS["brokers"][0] if a not in _REPORTS and a not in _DEADLINES), _CARRIER),
    ("setup", "Bordereau setup", _GROUPS["setup"][0] + ("datamodel_mapping_proposed", "datamodel_mapping_started",
                                                  "extra_field_adopted", "extra_field_saved",
                                                  "mapping_task_resolved"), _CARRIER),
    ("programme_rules", "Programme rules", _GROUPS["programme_rules"][0], _ALL),
    ("rule_library", "Rule library", _LIBRARY, _CARRIER),
    ("report_operational", "Operational report", tuple(a for a in _REPORTS if a.startswith("monthly_")), _CARRIER),
    ("report_executive", "Executive report", tuple(a for a in _REPORTS if a.startswith("executive_")), _CARRIER),
    ("company", "Company", ("tenant_created", "tenant_updated"), _CARRIER),
    ("people", "Users and roles", _GROUPS["people"][0], _PARTY),
    ("auth", "Sign-in and passwords", _GROUPS["auth"][0], _ALL),
    ("downloads", "Downloads", ("download", "export"), _ALL),
    ("views", "Views", ("view", "read"), _ALL),
    # Kept for their rows' names; not offered (see NOT_OFFERED).
    ("audits", "Audits and findings", _GROUPS["audits"][0], _ALL),
    ("integrity", "Integrity and retention", _GROUPS["integrity"][0], _CARRIER),
    ("sessions", "Session renewals", _GROUPS["sessions"][0], ("platform",)),
)
_AREA = {k: (lbl, acts) for k, lbl, acts, _s in AREAS}
_AREA_KEY_OF: dict[str, str] = {}
for _k, _lbl, _acts, _s in AREAS:
    for _a in _acts:
        _AREA_KEY_OF.setdefault(_a, _k)

# Areas: the menu's modules, each a list of sub-areas. Their keys start "m_"
# so they never clash with a sub-area's.
MODULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("m_contracts", "Contracts", ("contracts", "treaties", "signing", "rules")),
    ("m_reinsurance", "Reinsurance", ("reinsurance_setup", "reinsurance_bdx")),
    ("m_bordereaux", "Bordereaux", ("files", "exceptions", "calendar", "policies", "intake")),
    ("m_setup", "Setup", ("brokers", "setup", "programme_rules", "rule_library")),
    ("m_reports", "Reports", ("report_operational", "report_executive")),
    ("m_company", "Company and users", ("company", "people", "auth")),
    ("m_access", "Downloads and views", ("downloads", "views")),
)
_MODULE = {k: (lbl, subs) for k, lbl, subs in MODULES}
_MODULE_OF = {sub: k for k, _l, subs in MODULES for sub in subs}
# An older link may still name one of these; it means the sub-areas it became.
_RENAMED = {"downloads_and_views": ("downloads", "views"), "reports": ("report_operational", "report_executive")}


def subs_of(key: str) -> tuple[str, ...]:
    """The sub-areas an Area or sub-area key stands for."""
    if key in _MODULE:
        return _MODULE[key][1]
    if key in _RENAMED:
        return _RENAMED[key]
    return (key,) if key in _AREA else ()


# ── Action: what kind of step, from the event's name ────────────────────────
# First match wins, so the specific words come before the general ones.
VERBS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("signin", "Signed in or out", ("login_success", "login_failed", "logout", "password", "forgot", "token_refresh")),
    ("downloaded", "Downloaded", ("download", "export")),
    ("viewed", "Viewed", ("_read", "_opened", "_viewed")),
    ("restored", "Restored", ("made_active_again", "restore", "undone", "reopened")),
    ("asked", "Rejected or changes asked", ("changes_requested", "rejected", "sent_back", "pushed_back",
                                               "declined", "dispute", "queried", "code_wrong")),
    ("signed", "Signed", ("signed", "signing_code", "signature_")),
    ("approved", "Approved", ("approved", "accepted", "agreed", "confirmed", "signed_off",
                                          "activated", "_live", "_bound", "waived", "completed")),
    ("removed", "Deleted or removed", ("deleted", "removed", "retired", "withdrawn", "revoked", "voided",
                                       "terminated", "discarded", "cleared", "detached", "lifted")),
    ("sent", "Sent", ("_sent", "submitted", "emailed", "delivered", "shared", "chased",
                                "resent", "released", "notice", "awaiting")),
    ("checked", "Checked", ("_run", "validation", "checked", "validated", "analyzed", "ingest",
                                   "polled", "tested", "output_generated", "prepared")),
    ("reminded", "Reminders", ("overdue", "due_soon", "due_today", "reminder",
                                            "delinquent", "deadline", "on_hold", "needed")),
    ("added", "Created", ("created", "added", "uploaded", "raised", "invited", "linked",
                                    "put_on", "attached", "drafted", "built", "generated", "proposed", "started",
                                    "adopted", "replied", "evidence", "entered", "set_up", "placed", "_issued")),
    ("changed", "Changed", ("updated", "changed", "edited", "saved", "corrected", "toggled", "enabled", "disabled",
                            "refreshed", "renewed", "extended", "mapped", "resolved", "endorse",
                            "decided", "applied", "status", "closed", "transferred", "rerun",
                            "skipped", "from_standard", "_term", "structure", "answers")),
)


# The access log's own names, said exactly.
_EXACT = {"download": "downloaded", "export": "downloaded", "view": "viewed", "read": "viewed"}


# Rows the request middleware wrote ("POST /reinsurance/setups/{id}/layout/upload"):
# the area from the path, the action from the method and the path's last word.
_PATH_AREAS = (("/reinsurance/setups", "reinsurance_setup"), ("/reinsurance", "reinsurance_bdx"),
               ("/treat", "treaties"), ("/esign", "signing"),
               ("/rule-library", "rule_library"), ("generate-rules", "rules"), ("/rules", "rules"),
               ("/programme-rules", "programme_rules"),
               ("/contract", "contracts"), ("/output-template", "setup"), ("/pipelines", "setup"),
               ("/bordereau-setup", "setup"), ("/direct", "setup"),
               ("/intake", "intake"), ("/itd", "intake"),
               ("/broker/users", "people"), ("/users", "people"), ("/team", "people"),
               ("/broker/calendar", "calendar"), ("/calendar", "calendar"), ("/policies", "policies"),
               ("/broker/bordereau", "files"), ("/runs", "files"), ("/export", "files"), ("/upload", "files"),
               ("/brokers", "brokers"), ("/programs", "brokers"), ("/parties", "brokers"),
               ("/tenant", "company"), ("/audits", "audits"), ("/integrity", "integrity"),
               ("/retention", "integrity"), ("/monthly-report", "report_operational"),
               ("/executive", "report_executive"), ("/auth", "auth"))
_PATH_VERBS = (("dispute", "asked"), ("decline", "asked"), ("send", "sent"), ("resend", "sent"),
               ("discard", "removed"), ("withdraw", "removed"), ("accept", "approved"),
               ("approve", "approved"), ("activate", "approved"), ("analyze", "checked"),
               ("check", "checked"), ("runs", "checked"), ("preview", "checked"), ("verify", "signed"),
               ("sign", "signed"), ("restore", "restored"), ("upload", "added"), ("read", "viewed"))


def _is_path(action: str) -> bool:
    return " /" in (action or "")


# A name added after this list was written still lands somewhere sensible.
_NAME_AREAS = (("treaty_", "treaties"), ("signed_treaty", "signing"), ("reinsurance_setup", "reinsurance_setup"),
               ("reinsurance_", "reinsurance_bdx"), ("programme_rule", "programme_rules"),
               ("rule_library", "rule_library"), ("rule", "rules"), ("signature", "signing"),
               ("signing", "signing"), ("contract", "contracts"), ("audit_", "audits"),
               ("intake", "intake"), ("itd_", "intake"), ("submission_", "calendar"), ("bdx_fix", "exceptions"),
               ("bdx_", "files"), ("bordereau_setup", "setup"), ("output_template", "setup"),
               ("policies", "policies"), ("executive_report", "report_executive"),
               ("report", "report_operational"), ("tenant_", "company"),
               ("broker", "brokers"), ("program", "brokers"), ("party_", "brokers"), ("user", "people"),
               ("retention", "integrity"), ("legal_hold", "integrity"))


def area_key_of(action: str) -> Optional[str]:
    if _is_path(action):
        path = action.split(" ", 1)[1].lower()
        return next((k for prefix, k in _PATH_AREAS if prefix in path), None)
    if action in _AREA_KEY_OF:
        return _AREA_KEY_OF[action]
    a = (action or "").lower()
    return next((k for mark, k in _NAME_AREAS if mark in a), None)


def _path_verb(action: str) -> Optional[str]:
    method, path = action.split(" ", 1)
    tail = path.rstrip("/").rsplit("/", 1)[-1].lower()
    for word, verb in _PATH_VERBS:
        if word in tail:
            return verb
    return {"DELETE": "removed", "PUT": "changed", "PATCH": "changed", "POST": "added"}.get(method.upper())


# The Action dropdown's order — the same in every Area (VERBS is the order
# names are matched in, which is not the order a reader scans).
VERB_ORDER = ("added", "changed", "removed", "sent", "approved", "asked", "signed",
              "restored", "checked", "reminded", "viewed", "downloaded", "signin")


def verb_of(action: str) -> Optional[str]:
    if _is_path(action):
        return _path_verb(action)
    a = (action or "").lower()
    if a in _EXACT:
        return _EXACT[a]
    for key, _lbl, marks in VERBS:
        if any(m in a for m in marks):
            return key
    return None


_VERB_LABEL = {k: lbl for k, lbl, _m in VERBS}
_VERB_ACTIONS = {k: tuple(sorted(n for n in KNOWN | set(_EXACT) if verb_of(n) == k)) for k, _l, _m in VERBS}
# A view was written as "download" before views had a name of their own; which
# it was is told by access_kind (see narrow), so both verbs read both names.
_VERB_ACTIONS["downloaded"] += ("view", "read")
_VERB_ACTIONS["viewed"] += ("download",)

STATUSES = (("done", "Completed"), ("attention", "Needs attention"), ("failed", "Failed, rejected or deleted"))
_STATUS_TONES = {"done": ("ok", "info", "muted"), "attention": ("warn",), "failed": ("bad",)}

MAX_FILTERED = 3000        # rows read when Programme or Status is chosen
_SAVE_GAP = timedelta(seconds=2)


# ── the filters ─────────────────────────────────────────────────────────────
_PATHS: dict = {"at": None, "names": ()}
_PATHS_FOR = timedelta(minutes=10)


def _stored_names(s) -> tuple[str, ...]:
    """Every action name stored so far that this module has no list entry for —
    request paths, and names added since (cached for ten minutes)."""
    from db import ActivityEvent
    now = datetime.utcnow()
    if _PATHS["at"] is None or now - _PATHS["at"] > _PATHS_FOR:
        try:
            _PATHS["names"] = tuple(a for (a,) in s.query(ActivityEvent.action).distinct().limit(5000) if a)
            _PATHS["at"] = now
        except Exception:  # noqa: BLE001 — the named rows still filter
            s.rollback()
    return tuple(a for a in _PATHS["names"] if a not in KNOWN)


def _all_stored(s) -> set[str]:
    _stored_names(s)
    return set(_PATHS["names"])


def _cats_of(sub: str) -> set[str]:
    """Which stores a sub-area's rows live in."""
    if sub in ("auth", "sessions"):
        return {"auth"}
    if sub in ("downloads", "views"):
        return {"access", "activity"}
    if sub == "exceptions":
        return {"decision", "activity"}
    return {"activity"}


def narrow(s, f: af.Filters, area: str = "", verb: str = "") -> af.Filters:
    """The trail's filters with an Area and an Action applied in SQL."""
    area, verb = (area or "").strip(), (verb or "").strip()
    if area in ("", "all") and verb in ("", "all"):
        return f
    paths = _stored_names(s)
    acts: Optional[set[str]] = None
    cats = set(af.CATEGORIES)
    if area and area != "all":
        subs = subs_of(area)
        if subs:
            acts = {a for sub in subs for a in _AREA[sub][1]} | {p for p in paths if area_key_of(p) in subs}
            cats &= set().union(*(_cats_of(sub) for sub in subs))
            if {"downloads", "views"} & set(subs):
                f.views = True
                if "views" in subs:
                    acts.add("download")          # a view was written as "download" before views had a name
                if len({"downloads", "views"} & set(subs)) == 1:
                    f.access_kind = "download" if "downloads" in subs else "view"
            if "sessions" in subs:
                f.include_session_renewals = True
        else:                                   # one event name, as before
            acts = {area}
            cats &= set(af.categories_for_group(area))
    if verb and verb != "all" and verb not in _VERB_ACTIONS:
        # An Area's own Action list: one or more event names ("a,b" when two
        # names read the same words).
        names = {n for n in verb.split(",") if n}
        acts = names if acts is None else acts & names
        verb = ""
        cats &= {"activity", "decision"} if "exception_decided" in names else \
            {"auth"} if names & set(_GROUPS["auth"][0]) else \
            {"access", "activity"} if names & {"download", "view", "read", "export"} else {"activity"}
        if names & {"download", "view", "read"}:
            f.views = True
    if verb and verb != "all":
        vacts = set(_VERB_ACTIONS.get(verb, ())) | {p for p in paths if verb_of(p) == verb}
        vcats = {"activity"}
        if verb == "signin":
            vcats = {"auth"}
        elif verb in ("downloaded", "viewed"):
            vcats = {"access", "activity"}
            f.views = True
            f.access_kind = "download" if verb == "downloaded" else "view"
        elif verb == "changed":
            vcats = {"activity", "decision"}
        acts = vacts if acts is None else acts & vacts
        cats &= vcats
    acts = (acts or set()) - set(af.SUPPRESSED_ACTIONS)
    f.actions = tuple(sorted(acts or {"__nothing__"}))
    f.categories = tuple(c for c in af.CATEGORIES if c in cats) or ("activity",)
    return f


# Not offered (20 Nov 2026, user request). Their rows still show in the list,
# and a link naming them (?action=audits) still filters. Session renewals are
# the browser keeping someone signed in, not a person acting.
NOT_OFFERED = {"audits", "integrity", "sessions"}

# What each sub-area holds, for the ⓘ beside Area.
AREA_HELP = {
    "contracts": "Contracts with brokers: drafted, reviewed, changed, renewed, mid-term changes, documents attached.",
    "treaties": "Contracts with reinsurers: shared, approved or changes asked, term changes, endorsements.",
    "signing": "E-signatures on broker and reinsurance contracts: sent for signing, codes, signed, declined.",
    "rules": "The checks Kavachio builds from contract clauses: built, changed, switched on or off.",
    "reinsurance_setup": "Reinsurance bordereau setups: saved, sent for approval, sent back, activated.",
    "reinsurance_bdx": "Reinsurance bordereaux: prepared, checked, sent, agreed or queried by reinsurers, and their errors.",
    "files": "Files sent in, checked and delivered, and the status emails about them.",
    "exceptions": "Errors found in a bordereau and how the broker fixed, approved or dismissed them.",
    "calendar": "Due dates, overdue reminders, chasers and disputes about late files.",
    "policies": "Policy lists downloaded or emailed from Policy Explorer.",
    "intake": "How brokers send files by email, SFTP or API: the addresses, folders and keys set up for them, "
              "connection tests, and the files that came in this way.",
    "brokers": "Programmes created, brokers added or removed, deadlines and delivery settings.",
    "setup": "Bordereau setups, column mappings and output templates.",
    "programme_rules": "Rules set for a whole programme and sent to brokers or reinsurers to approve.",
    "rule_library": "The carrier's own library of checks: added, changed, switched on or off, deleted.",
    "report_operational": "The operational report: its settings and each send.",
    "report_executive": "The executive report: its settings and each send.",
    "company": "The company's own details.",
    "people": "Users invited, changed or removed, and what they can see.",
    "auth": "Sign-ins, failed sign-ins, sign-outs and password resets.",
    "downloads": "Who downloaded a file, contract or report.",
    "views": "Who opened a file, contract or report to look at it.",
}


def options(s, v: af.Viewer) -> dict:
    """Area (the menu's modules) → Sub-area, and each one's Action and Status
    lists, for this seat. A module none of whose parts this seat has is left out."""
    seats = {k: st for k, _l, _a, st in AREAS}
    stored = _all_stored(s) if s is not None else set()
    modules, actions, statuses = [], {}, {}
    for key, label, subs in MODULES:
        mine = [x for x in subs if v.seat in seats[x] and x not in NOT_OFFERED]
        if not mine:
            continue
        modules.append({"key": key, "label": label,
                        "subs": [{"key": x, "label": _AREA[x][0], "info": AREA_HELP.get(x, "")} for x in mine]})
        for x in mine:
            actions[x] = _area_actions((x,), stored)
            statuses[x] = _area_statuses((x,))
        actions[key] = _area_actions(tuple(mine), stored)
        statuses[key] = _area_statuses(tuple(mine))
    every = sorted({w for k, ws in statuses.items() if k.startswith("m_") for w in ws}, key=str.lower)
    return {"modules": modules,
            "verbs": [{"key": k, "label": _VERB_LABEL[k]} for k in VERB_ORDER],
            "area_actions": actions,
            "statuses": [{"key": w, "label": w} for w in every],
            "area_statuses": {k: [{"key": w, "label": w} for w in ws] for k, ws in statuses.items()}}


_NOT_IN_ACTIVITY = set(_GROUPS["auth"][0]) | {"exception_decided", "download", "view", "read", "export"}


def _area_actions(subs: tuple[str, ...], stored: set[str]) -> list[dict]:
    """The Action list for an Area or sub-area: the same short list as
    everywhere, in the same order, keeping only the kinds of step that happen
    there (and have happened at least once) — no "Signed in or out" under
    Broker contracts."""
    names = {a for x in subs for a in _AREA[x][1]} - set(af.SUPPRESSED_ACTIONS) - set(af.NEVER_SHOWN_ACTIONS)
    if stored:
        # Names kept outside activity_events (sign-ins, downloads, decisions)
        # are offered as they are; the rest only once they have happened.
        names = {n for n in names if n in stored or n in _NOT_IN_ACTIVITY}
        names |= {n for n in stored if _is_path(n) and area_key_of(n) in subs
                  and n not in af.SUPPRESSED_ACTIONS}
    there = {verb_of(n) for n in names}
    return [{"key": k, "label": _VERB_LABEL[k]} for k in VERB_ORDER if k in there]


# Badges that come from what a row recorded, not from its name.
_EXTRA_STATUSES = {
    "files": ("Clean", "Flagged", "Not Validated", "Email not sent"),
    "calendar": ("Reminder",),
    "auth": ("Successful", "Failed"),
    "downloads": ("Downloaded",),
    "views": ("Viewed",),
    "exceptions": tuple(w for w, _t in af.DECISION_STATUS.values()) + ("Resolved",),
}


def _area_statuses(subs: tuple[str, ...]) -> list[str]:
    """Every Status badge rows of these sub-areas can carry."""
    words: set[str] = set()
    for x in subs:
        if x in ("downloads", "views"):
            words |= set(_EXTRA_STATUSES[x])
            continue
        cat = "auth" if x == "auth" else "activity"
        words |= {af._status_for(cat, n, {})[0] for n in _AREA[x][1]}
        words |= set(_EXTRA_STATUSES.get(x, ()))
    return sorted(words, key=str.lower)


# ── the rows ────────────────────────────────────────────────────────────────
def _programmes_of(s, raws: list[dict]) -> dict[int, Optional[int]]:
    """Which programme each raw row is about, where the row says (by index)."""
    from db import Contract, DirectFormat, ExceptionDecisionLog, ExportTemplate
    want: dict[int, tuple[str, int]] = {}
    out: dict[int, Optional[int]] = {}
    for i, r in enumerate(raws):
        d = r.get("details") if isinstance(r.get("details"), dict) else {}
        t = r.get("target") or ""
        pid = d.get("program_id")
        if isinstance(pid, int) or (isinstance(pid, str) and pid.isdigit()):
            out[i] = int(pid)
            continue
        if t.startswith("program:") and t[8:].isdigit():
            out[i] = int(t[8:])
            continue
        if r["kind"] == "decision":
            want[i] = ("decision", r["id"])
            continue
        for key, kind in (("contract_id", "contract"), ("format_id", "format"),
                          ("template_id", "template"), ("output_template_id", "template")):
            val = d.get(key)
            if isinstance(val, int):
                want[i] = (kind, val)
                break
        else:
            for prefix, kind in (("contract:", "contract"), ("format:", "format")):
                if t.startswith(prefix) and t[len(prefix):].isdigit():
                    want[i] = (kind, int(t[len(prefix):]))
                    break
    by_kind: dict[str, set[int]] = {}
    for kind, ident in want.values():
        by_kind.setdefault(kind, set()).add(ident)
    found: dict[tuple[str, int], Optional[int]] = {}
    for kind, model in (("contract", Contract), ("format", DirectFormat),
                        ("template", ExportTemplate), ("decision", ExceptionDecisionLog)):
        ids = sorted(by_kind.get(kind, ()))
        if ids:
            for ident, pid in s.query(model.id, model.program_id).filter(model.id.in_(ids)).all():
                found[(kind, ident)] = pid
    for i, key in want.items():
        out[i] = found.get(key)
    return out


def _num(text) -> str:
    """The record number at the end of a target ("library_rule:57", "57")."""
    m = re.search(r"(\d+)\s*$", str(text or ""))
    return m.group(1) if m else ""


def _saves_for(s, sc, raws: list[dict]) -> dict[int, list[dict]]:
    """The before / after recorded for the rows' saves.

    A save writes a record_changed row (change_history.py) in the same second
    as the trail's own row for it — often without the person's id on the
    named row, and with the record written differently ("57" against
    "library_rule:57"). So each recorded save goes to the closest row within
    _SAVE_GAP: the same record number first, a named row over a request copy,
    and never a row done by someone else. Index → the changed fields."""
    from db import ActivityEvent
    import change_history as chist
    acts = [(i, r) for i, r in enumerate(raws) if r["kind"] == "activity" and r.get("at")]
    if not acts:
        return {}
    lo = min(r["at"] for _i, r in acts) - _SAVE_GAP
    hi = max(r["at"] for _i, r in acts) + _SAVE_GAP
    q = af._base_query(s, ActivityEvent, sc, af.Filters(), ([], []))
    if q is None:
        return {}
    saves = (q.filter(ActivityEvent.action == chist.ACTION,
                      ActivityEvent.created_at.between(lo, hi))
             .order_by(ActivityEvent.id).limit(2000).all())
    out: dict[int, list[dict]] = {}
    for e in saves:
        best = None
        for i, r in acts:
            if r.get("user_id") and e.actor_user_id and r["user_id"] != e.actor_user_id:
                continue
            gap = abs((e.created_at - r["at"]).total_seconds())
            if gap > _SAVE_GAP.total_seconds():
                continue
            score = (0 if _num(r.get("target")) and _num(r.get("target")) == _num(e.target) else 1,
                     1 if _is_path(r["action"]) else 0, gap)
            if best is None or score < best[0]:
                best = (score, i)
        if best is None:
            continue
        d = e.details if isinstance(e.details, dict) else {}
        out.setdefault(best[1], []).extend(
            {"field": c.get("field"), "old": c.get("old"), "new": c.get("new")}
            for c in (d.get("changes") or []) if isinstance(c, dict))
    return out


def _finish(s, v: af.Viewer, sc, raws: list[dict]) -> list[dict]:
    """audit_feed's own finishing steps, then Area and Old / New."""
    namer = af.ActorNamer(s, v)
    af._link_people(s, raws)
    af._link_versions(s, raws)
    af._run_exports(s, raws)
    af._sign_targets(s, raws)
    af._twin_people(s, raws)
    names = af._names_for(s, v, raws)
    saves = _saves_for(s, sc, raws)
    items = []
    for i, raw in enumerate(raws):
        row = af.render(s, namer, raw, names)
        key = ("downloads" if raw["kind"] == "access" else "auth" if raw["kind"] == "auth"
               else "exceptions" if raw["kind"] == "decision" else area_key_of(raw["action"]))
        if raw["kind"] == "access":
            key = "downloads" if row.get("status") == "Downloaded" else "views"
        row["area"] = _AREA[key][0] if key in _AREA else None
        row["module"] = _MODULE[_MODULE_OF[key]][0] if key in _MODULE_OF else None
        ch = saves.get(i) or []
        row["changes"] = ch
        if ch:
            first = ch[0]
            more = f" (+{len(ch) - 1} more)" if len(ch) > 1 else ""
            label = f"{first['field']}: " if first.get("field") else ""
            row["old"] = f"{label}{first['old'] if first.get('old') is not None else '—'}"
            row["new"] = f"{label}{first['new'] if first.get('new') is not None else '—'}{more}"
        else:
            row["old"] = row["new"] = None
        items.append(row)
    return items


def _seen_text(row: dict, tz: int) -> str:
    """Everything the table shows for a row, as one lower-case string — what
    Search looks through. The time is written the ways a screen may show it, in
    the reader's own timezone (`tz`: minutes east of UTC, from the browser)."""
    parts = [row.get(k) for k in ("actor", "actor_role", "actor_org", "area", "action_label", "detail",
                                  "target_kind", "target", "old", "new", "status", "carrier")]
    for c in row.get("changes") or []:
        parts += [c.get("field"), c.get("old"), c.get("new")]
    at = row.get("at")
    if at:
        try:
            when = datetime.fromisoformat(at.replace("Z", "+00:00")).replace(tzinfo=None) + timedelta(minutes=tz)
            parts += [when.strftime("%d %b %Y, %H:%M"), when.strftime("%d %b %Y, %I:%M %p"),
                      when.strftime("%d/%m/%Y"), when.strftime("%Y-%m-%d")]
        except ValueError:
            pass
    return " ".join(str(p) for p in parts if p).lower()


def _matches(row: dict, q: str, tz: int) -> bool:
    """Every word typed appears somewhere in what the row shows."""
    text = _seen_text(row, tz)
    return all(w in text for w in q.lower().split())


def page(s, v: af.Viewer, f: af.Filters, page_no: int, page_size: int,
         programme: Optional[int] = None, status: str = "", tz: int = 0) -> dict:
    """One page of the one list, newest first."""
    page_no = max(1, page_no)
    page_size = max(1, min(page_size, af.MAX_PAGE_SIZE))
    offset = min((page_no - 1) * page_size, af.MAX_OFFSET)
    sc = af.scope_for(s, v)
    # Search looks at what the table SHOWS (names, words, values, dates), which
    # exists only once a row is finished — so it is done here, not in SQL.
    q = (f.q or "").strip()
    f = replace(f, q="")
    post = bool(programme) or bool(status) or bool(q)
    raw, total = af._rows(s, sc, f, MAX_FILTERED if post else offset + page_size)
    if post:
        raw = _keep(s, v, sc, raw[:MAX_FILTERED], programme, status, q, tz)
        total = len(raw)
        shown = raw[offset:offset + page_size]
        items = shown if shown and "area" in shown[0] else _finish(s, v, sc, shown)
    else:
        items = _finish(s, v, sc, raw[offset:offset + page_size])
    return {"items": items, "total": total, "page": page_no, "page_size": page_size, "seat": v.seat}


def _keep(s, v, sc, raws: list[dict], programme: Optional[int], status: str,
          q: str = "", tz: int = 0) -> list:
    """Programme first (cheap, on the raw rows), then Status and Search (they
    need the finished row, so the survivors are finished here and returned finished)."""
    if programme:
        pids = _programmes_of(s, raws)
        raws = [r for i, r in enumerate(raws) if pids.get(i) == programme]
    if status or q:
        return [row for row in _finish(s, v, sc, raws)
                if (not status or _status_ok(row, status)) and (not q or _matches(row, q, tz))]
    return raws


def _status_ok(row: dict, status: str) -> bool:
    """The Status filter: a badge's words, exactly — or one of the three
    groups an older link may still carry."""
    if status in _STATUS_TONES:
        return row["tone"] in _STATUS_TONES[status]
    return (row.get("status") or "").lower() == status.lower()


def export_rows(s, v: af.Viewer, f: af.Filters, programme: Optional[int] = None,
                status: str = "", tz: int = 0) -> list[dict]:
    """The download: every row the filters select, one line per decision."""
    sc = af.scope_for(s, v)
    q = (f.q or "").strip()
    f = replace(f, q="")
    raw, _t = af._rows(s, sc, f, af.MAX_EXPORT_ROWS, grouped=False)
    raw = raw[:af.MAX_EXPORT_ROWS]
    af._tag_bulk(raw)
    if programme:
        pids = _programmes_of(s, raw)
        raw = [r for i, r in enumerate(raw) if pids.get(i) == programme]
    rows = _finish(s, v, sc, raw)
    if status:
        rows = [r for r in rows if _status_ok(r, status)]
    if q:
        rows = [r for r in rows if _matches(r, q, tz)]
    return rows


# ── one entry in full: the Event details panel ─────────────────────────────
# Stored keys that say nothing to a reader, or are already shown elsewhere in
# the panel (who, when, the badge, the record), or are internal numbers.
_PANEL_SKIP = {"status", "ip", "method", "email", "user_agent", "full_name", "path", "event", "key", "name", "role",
               "changes", "more", "before", "after", "rule", "entity", "entity_id", "module", "kind", "target",
               "link", "token", "mail_error", "unresolved"}
_PANEL_WORDS = {
    "filename": "File name", "source_filename": "Source file", "name": "Name", "rows": "Rows",
    "row_count": "Rows", "exceptions": "Errors found", "flagged": "Has errors", "unresolved_count": "Errors still open",
    "period": "Period", "due_date": "Due date", "version": "Version", "channel": "Sent by",
    "recipient": "Sent to", "via": "Through", "reason": "Reason given", "note": "Note", "reference": "Reference",
    "program_name": "Programme", "broker_name": "Broker", "loaded": "Rows saved", "failed": "Rows not saved",
    "total": "Rows in all", "rule_count": "Checks", "format": "Format", "scope": "Scope", "calendar": "Calendar",
    "saved": "Saved", "mail": "Email", "error": "Problem", "party_type": "Kind of company", "lane": "Lane",
    "sheet_count": "Sheets", "policies": "Policies", "count": "Items", "changed": "Fields changed",
}
_EARLIER = 5


def _plain(key: str) -> str:
    return _PANEL_WORDS.get(key) or key.replace("_", " ").strip().capitalize()


def _value(v) -> Optional[str]:
    if v is None or v == "" or v == [] or v == {}:
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (list, tuple)):
        if all(isinstance(x, (str, int, float)) for x in v):
            words = [str(x).replace("_", " ") for x in v[:12]]
            return ", ".join(words) + (f" (+{len(v) - 12} more)" if len(v) > 12 else "")
        return f"{len(v)} items"
    if isinstance(v, dict):
        return None
    text = str(v)
    return text if len(text) <= 160 else text[:159] + "…"


def _more_details(details) -> list[dict]:
    """Everything else the entry recorded, in plain words. Ids ("…_id") are
    internal numbers and are left out; their records are named elsewhere."""
    d = details if isinstance(details, dict) else {}
    out = []
    for k, v in d.items():
        if k in _PANEL_SKIP or k.endswith("_id") or k.endswith("_ids"):
            continue
        shown = _value(v)
        if shown is not None:
            out.append({"label": _plain(k), "value": shown})
    return out


def _raw_by_id(s, sc, kind: str, ident: int) -> Optional[dict]:
    """One stored entry as a feed row — through the viewer's own scope, so an
    id typed by hand opens nothing the list would not have shown."""
    from db import AccessLog, ActivityEvent, AuthAudit
    f = af.Filters()
    if kind == "activity":
        q = af._base_query(s, ActivityEvent, sc, f, ([], []))
        e = q.filter(ActivityEvent.id == ident).first() if q is not None else None
        return af._activity_raw(e) if e else None
    if kind == "auth":
        q = af._base_query(s, AuthAudit, sc, f, ([], []))
        e = q.filter(AuthAudit.id == ident).first() if q is not None else None
        return None if e is None else {
            "kind": "auth", "id": e.id, "at": e.created_at, "action": e.event, "target": None,
            "details": e.details, "detail": af._detail_words(e.details, "auth"), "actor": e.actor,
            "user_id": e.user_id, "role": None, "broker_party_id": None, "tenant_id": e.tenant_id,
            "ip": e.ip, "ok": e.ok}
    if kind == "access":
        q = af._base_query(s, AccessLog, sc, f, ([], []))
        e = q.filter(AccessLog.id == ident).first() if q is not None else None
        return None if e is None else {
            "kind": "access", "id": e.id, "at": e.created_at, "action": e.action, "target": e.resource,
            "details": None, "detail": "", "actor": e.actor, "user_id": e.user_id, "role": None,
            "broker_party_id": None, "tenant_id": e.tenant_id, "ip": e.ip, "ok": None}
    if kind == "decision":
        from db import ExceptionDecisionLog as E
        q = af._decision_query(s, sc, f, ([], []))
        r = q.filter(E.id == ident).first() if q is not None else None
        return af._decision_raw(r) if r else None
    return None


def _save_reason(s, sc, raw: dict) -> Optional[str]:
    """The reason typed for this save, if its change-history row has one."""
    from db import ActivityEvent
    import change_history as chist
    if raw["kind"] != "activity" or not raw.get("at"):
        return None
    q = af._base_query(s, ActivityEvent, sc, af.Filters(), ([], []))
    if q is None:
        return None
    for e in (q.filter(ActivityEvent.action == chist.ACTION,
                       ActivityEvent.created_at.between(raw["at"] - _SAVE_GAP, raw["at"] + _SAVE_GAP))
              .limit(20).all()):
        d = e.details if isinstance(e.details, dict) else {}
        if d.get("reason") and (not raw.get("user_id") or not e.actor_user_id
                                or e.actor_user_id == raw["user_id"]):
            return str(d["reason"])
    return None


def event(s, v: af.Viewer, row_id: str, tz: int = 0) -> Optional[dict]:
    """One entry in full, for the Event details panel. `row_id` is the list's
    own id ("activity:55284"). None when it is not one this viewer may read."""
    from db import ActivityEvent, Contract, Program
    kind, _sep, num = (row_id or "").partition(":")
    if not num.isdigit():
        return None
    sc = af.scope_for(s, v)
    raw = _raw_by_id(s, sc, kind, int(num))
    if raw is None:
        return None
    row = _finish(s, v, sc, [raw])[0]
    who_key = row.get("actor_role_key") or ""
    # No signed-in person but an email address: someone answering a link they
    # were sent (a reinsurer on a treaty, a signer, a broker on the review link).
    by_link = (raw.get("via_email") or raw.get("via_link")
               or (not raw.get("user_id") and "@" in str(raw.get("actor") or "")))
    how = ("Kavachio on its own" if who_key == "system"
           else "From the link in an email" if by_link else "In the portal")
    d = raw.get("details") if isinstance(raw.get("details"), dict) else {}

    pid = _programmes_of(s, [raw]).get(0)
    programme = s.query(Program.name).filter(Program.id == pid).scalar() if pid else None
    cid = d.get("contract_id") if isinstance(d.get("contract_id"), int) else None
    t = raw.get("target") or ""
    if cid is None and t.startswith("contract:") and t[9:].isdigit():
        cid = int(t[9:])
    contract = None
    if cid:
        c = s.query(Contract).filter(Contract.id == cid).first()
        contract = (getattr(c, "name", None) or getattr(c, "filename", None) or f"Contract #{cid}") if c else None

    # Earlier on the same record: same stored target, in scope, before this one.
    earlier, record = [], None
    if raw["kind"] == "activity" and t:
        q = af._base_query(s, ActivityEvent, sc, af.Filters(), ([], []))
        if q is not None:
            prev = (q.filter(ActivityEvent.target == t, ActivityEvent.id != raw["id"],
                             ActivityEvent.created_at <= raw["at"],
                             ActivityEvent.action.notin_(af.NEVER_SHOWN_ACTIONS + af.SUPPRESSED_ACTIONS))
                    .order_by(ActivityEvent.created_at.desc(), ActivityEvent.id.desc()).limit(_EARLIER).all())
            if prev:
                for r in _finish(s, v, sc, [af._activity_raw(e) for e in prev]):
                    earlier.append({"at": r["at"], "who": r["actor"], "what": r["action_label"],
                                    "status": r["status"], "tone": r["tone"]})
        record = f"contract:{cid}" if cid else None

    return {
        "row": row,
        "who": {"person": row["actor"], "role": row["actor_role"], "organisation": row.get("actor_org"),
                "carrier": row.get("carrier"), "how": how, "ip": raw.get("ip")},
        "what": {"area": row.get("module"), "sub_area": row.get("area"),
                 "action_type": _VERB_LABEL.get(verb_of(raw["action"]) or "", None),
                 "on": (f"{row['target_kind']}: {row['target']}" if row.get("target_kind") else row.get("target"))
                 if row.get("target") and row["target"] != "—" else None,
                 "programme": programme, "contract": contract},
        "changes": row.get("changes") or [],
        "reason": _save_reason(s, sc, raw),
        "more": _more_details(d),
        "earlier": earlier,
        "record": record, "search": None if record else (row.get("target") if earlier else None),
        "reference": f"{raw['kind']} {raw['id']}",
    }
