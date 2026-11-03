"""
Mid-term change — the routes (Kavachio_Mid_Term_Change_Plan.md §4–§13).

The contract stays the same contract. A mid-term change is a contract_amendment
row that goes to the broker, is agreed and signed — or, for an uploaded
contract with a signed change document, accepted — and only then goes live.
Until it is live nothing about the contract or its checks changes: files keep
being checked on the current terms (decision 2).

Going live, in one transaction:
  * every check the change touches is CLOSED — effective_to = the change's
    effective date (exclusive, so it still covers the day before) — and its
    new version OPENED from that date, with source_amendment_id set;
  * checks the change does not touch are not touched: same ids, same on/off
    state, same setup exclusions;
  * a contract written in Kavachio gets its new terms (commercial_terms);
  * the broker is emailed after the commit.
Each bordereau row is then checked by the version in force on its transaction
effective date — rule_windows.py and duckdb_validation._DatedRun.

The rules of all this live in mid_term_change.py; this module reads the facts,
asks it, and writes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import text

import contract_routes as cr
import contract_types as ct
import mid_term_ai as mta
import mid_term_change as mt
import rule_windows as rw
from auth_deps import Principal, current_principal
from db import (
    Contract, ContractAmendment, ContractApproval, ContractDocument,
    ContractSignature, ExportTemplate, Party, Pipeline, PipelineContract,
    Program, SessionLocal,
)

log = logging.getLogger("kavachio.contracts.mid_term")
router = APIRouter(tags=["contracts"])


class MidTermIn(BaseModel):
    """What the carrier is changing, and from when."""
    effective_from: Optional[str] = None
    # Shown to the broker, in the change document and the email.
    reason: Optional[str] = None
    # The contract's terms AS THEY WILL BE — the panel starts from the current
    # ones and the carrier edits what moved.
    agreed_limits: dict = {}
    # "Kavachio isn't sure": {term key: [rule ids this change replaces]}.
    choices: dict = {}
    # {setup (pipeline) id: the output column holding each row's transaction
    # effective date} — saved on the setup when the change is sent.
    date_columns: dict = {}
    # The change document's sections, as the carrier edited them. Absent: use
    # what is generated.
    sections: Optional[list] = None
    # The clauses, as the carrier edited them — on the clauses as they read
    # today: [{ref ("c:<clause id>" for an uploaded contract, "s:<section key>"
    # for one written here; none for a new clause), action: amended | added |
    # removed, title, text, section}]. What actually moved is worked out by
    # mid_term_change.clause_changes.
    clauses: list = []
    # A change document the carrier uploaded (POST …/mid-term/document). It is
    # THE document: Kavachio composes none.
    document_id: Optional[int] = None
    # …and it is already signed by both sides: no broker round — the carrier
    # accepts it and it goes live (plan §4, Road 2).
    signed: bool = False
    # Ask the model for the checks re-worded and new clauses make. The preview
    # leaves it off unless asked (it runs as the carrier types); Send always
    # works them out.
    work_out_checks: bool = False


class MidTermNote(BaseModel):
    note: Optional[str] = None


# =============================================================================
#  Facts
# =============================================================================

def _gate(s, p: Principal, contract_id: int) -> Contract:
    """A contract this seat may open (_contract_access refuses Kavachio's
    seat at the door and a broker another broker's)."""
    return cr._contract_access(s, p, contract_id)


def _lock(s, c: Contract) -> Contract:
    """The contract row, locked and read again, so two changes (or a change and
    a date correction) cannot pass their checks at the same moment."""
    return (s.query(Contract).filter(Contract.id == c.id)
            .with_for_update().populate_existing().one())


NO_CHECKS_WORDS = (
    "This contract has no checks yet — it was never set up in Bordereau Setup. A "
    "mid-term change dates the checks it moves, so set the contract up first: on "
    "its contract page, make its checks with the existing setup's templates, or "
    "build a new setup.")


def _amendments(s, c: Contract) -> list[ContractAmendment]:
    return (s.query(ContractAmendment)
            .filter(ContractAmendment.contract_id == c.id,
                    ContractAmendment.kind == mt.KIND)
            .order_by(ContractAmendment.id.desc()).all())


def _open_amendment(s, c: Contract) -> Optional[ContractAmendment]:
    return next((a for a in _amendments(s, c) if a.status in mt.OPEN), None)


def _amendment(s, c: Contract, amendment_id: int) -> ContractAmendment:
    a = s.get(ContractAmendment, amendment_id)
    if a is None or a.contract_id != c.id or a.kind != mt.KIND:
        raise HTTPException(404, "mid-term change not found")
    return a


def _latest_live_from(s, c: Contract) -> Optional[dt.date]:
    days = [a.effective_date for a in _amendments(s, c)
            if a.status == mt.LIVE and a.effective_date]
    return max(days) if days else None


def _rules_of(s, c: Contract) -> list[dict]:
    import rule_scope
    rows = s.execute(text(
        "SELECT rule_id, rule_name, rule_spec, rule_status, error_message, "
        "rule_description, severity, effective_from, effective_to, "
        f"source_amendment_id, source_clause_id, {rule_scope.tag_select(s)} "
        "FROM validation_rule WHERE contract_id = :c ORDER BY rule_id"),
        {"c": c.id}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["output_template_id"] = d.pop("rule_template_id", None)
        if isinstance(d.get("rule_spec"), str):
            try:
                d["rule_spec"] = json.loads(d["rule_spec"])
            except ValueError:
                pass
        out.append(d)
    return out


def _setups(s, c: Contract) -> list[Pipeline]:
    """The Bordereau Setups that run this contract (pinned to it), other than
    ones already replaced."""
    pids = sorted({pc.pipeline_id for pc in s.query(PipelineContract)
                   .filter(PipelineContract.contract_id == c.id).all()})
    if not pids:
        return []
    return [pp for pp in s.query(Pipeline).filter(Pipeline.id.in_(pids))
            .order_by(Pipeline.id).all()
            if (getattr(pp, "status", None) or "") != "superseded"]


def _own_template_id(s, c: Contract) -> Optional[int]:
    if c.output_template_id:
        return c.output_template_id
    for pp in _setups(s, c):
        if pp.output_template_id:
            return pp.output_template_id
    return None


def _structure(s, template_id: Optional[int]) -> dict:
    if not template_id:
        return {}
    t = s.get(ExportTemplate, template_id)
    if t is None:
        return {}
    from direct_routes import _load_structure
    return _load_structure(t) or {}


def _fields(s, template_id: Optional[int]) -> list[dict]:
    from app_routes import _template_fields_from_structure
    t = s.get(ExportTemplate, template_id) if template_id else None
    return _template_fields_from_structure(t.structure) if t is not None else []


def _fields_by_set(s, c: Contract, rules: list[dict]) -> tuple[dict, dict]:
    """{rule set → template fields}, {rule set → template id}. The contract's
    own set is None and is checked against its own template; a set added for
    another template (rule_scope.py) against that one."""
    own = _own_template_id(s, c)
    tids = {None: own}
    for r in rules:
        if r.get("output_template_id") is not None:
            tids[r["output_template_id"]] = r["output_template_id"]
    return ({k: _fields(s, v) for k, v in tids.items()}, tids)


def _compiler(fields: list[dict]):
    """The rule compiler bound to one template's columns — the same compiler
    and the same fan-out every other rule of the contract was compiled with."""
    field_to_sheet: dict = {}
    for f in fields:
        if f.get("name") and f.get("sheet"):
            field_to_sheet.setdefault(f["name"], [])
            if f["sheet"] not in field_to_sheet[f["name"]]:
                field_to_sheet[f["name"]].append(f["sheet"])
    first = next((v[0] for v in field_to_sheet.values() if v), None)

    def run(ir: dict) -> Optional[str]:
        from contract_upload_services.rule_compiler import compile_ir
        try:
            return compile_ir(ir, field_to_sheet, default_sheet=first)
        except Exception as e:  # noqa: BLE001 — reported as a check that can't move
            log.warning("mid-term change: %r did not compile: %s", ir.get("rule_name"), e)
            return None
    return run


def _term_rule(rule: dict, template_id: Optional[int]) -> dict:
    """contract_rules' rule as the columns a validation_rule row needs — the
    same rule_spec shape contract_rules.write_rules stores."""
    return {
        "rule_name": rule["name"], "rule_description": rule["description"],
        "severity": rule["severity"], "error_message": rule["error_message"],
        "canonical_target": {"output_field": rule["column"], "sheet": rule.get("sheet"),
                             "canonical_field": rule.get("canonical_field")},
        "rule_spec": {
            "source": "contract_terms", "limit": rule["key"],
            "operator": rule["operator"], "operand": rule["operand"],
            "output_template_id": template_id, "ir": rule.get("ir"),
            "template": (rule.get("ir") or {}).get("template"),
            "params": (rule.get("ir") or {}).get("params"),
            "compiled_sql": rule.get("compiled_sql")},
    }


def _old_terms(s, c: Contract, rules: list[dict], fields: list[dict],
               day: Optional[dt.date], read: str = "cached") -> tuple[dict, Optional[str]]:
    """(the terms the change starts from, why some could not be read).

    Typed terms for a contract written in Kavachio. For an uploaded one: the
    terms its checks hold now (mid_term_change.current_terms_from_rules), and,
    for every term no check holds, the value its clauses state
    (mid_term_ai.read_terms) — so a contract with no Bordereau Setup yet still
    opens with its terms filled in. `read` "ask" calls the model on a miss
    (the page's first load); "cached" uses only a stored answer (the preview,
    Send and go-live, which must never wait on — or differ by — a new call).
    Each term read off a clause carries that clause's id, so the clause can be
    re-worded with the term."""
    import contract_rules as crr
    if not cr._is_uploaded(c):
        return ct.clean_agreed_limits(c.commercial_terms or {}), None
    derived = mt.current_terms_from_rules(
        rules, fields, ct.AGREED_LIMITS, crr.LIMIT_COLUMNS, crr.LIMIT_OPERATORS,
        crr._find_column, day or cr._today())
    clean = ct.clean_agreed_limits(derived)
    for k in clean:
        clean[k]["read_from_checks"] = True
    read_, err = mta.read_terms(_current_clauses(s, c, rules, day), c.tenant_id,
                                ask=(read == "ask"))
    for k, v in read_.items():
        if k in clean:
            clean[k]["clause_id"] = v.get("clause_id")
            continue
        entry = ct.clean_agreed_limits({k: {"value": v["value"]}}).get(k)
        if entry:
            clean[k] = {**entry, "read_from_clause": True,
                        "clause_id": v.get("clause_id"), "quote": v.get("quote")}
    return clean, err


_MARKS = ("read_from_checks", "read_from_clause", "clause_id", "quote")


def _without_marks(terms: dict) -> dict:
    return {k: {kk: vv for kk, vv in (v or {}).items() if kk not in _MARKS}
            for k, v in (terms or {}).items()}


def _next_number(s, c: Contract) -> int:
    """After every change already made, and after any endorsement document
    attached before mid-term changes existed (it holds a number too)."""
    legacy = [d for d in (s.query(ContractDocument)
                          .filter(ContractDocument.contract_id == c.id,
                                  ContractDocument.kind == "endorsement",
                                  ContractDocument.amendment_id.is_(None)).all())
              # an uploaded change document not yet sent is not a change
              if not (isinstance(d.extracted, dict) and d.extracted.get("mid_term_upload"))]
    return 1 + len(legacy) + len(_amendments(s, c))


def _recheck_files(s, c: Contract, day: Optional[dt.date]) -> list[dict]:
    """Files already checked under this contract that may hold rows dated on or
    after the change: those for a reporting period ending on or after it (or,
    with no period recorded, produced on or after it). Listed, never re-run
    (decision D6)."""
    if not day:
        return []
    rows = s.execute(text(
        "SELECT DISTINCT oe.id, oe.filename, oe.created_at, es.period, es.period_end "
        "FROM output_exports oe "
        "LEFT JOIN submission_version sv ON sv.received_export_id = oe.id "
        "LEFT JOIN expected_submission es ON es.id = sv.expected_id "
        "WHERE oe.contract_id = :c AND (es.period_end >= :d "
        "  OR (es.period_end IS NULL AND oe.created_at >= :d)) "
        "ORDER BY oe.created_at DESC LIMIT 200"), {"c": c.id, "d": day}).mappings().all()
    return [{"export_id": r["id"], "filename": r["filename"], "period": r["period"],
             "period_end": str(r["period_end"]) if r["period_end"] else None,
             "checked_at": cr._iso_utc(r["created_at"])} for r in rows]


# =============================================================================
#  Clauses — what the contract says, as it reads today
# =============================================================================

def _deleted_clause_ids(s, c: Contract) -> set[int]:
    """Clauses a live change deleted. The row stays — the checks it closed
    still name it — but it is no longer part of what the contract says."""
    out: set[int] = set()
    for a in _amendments(s, c):
        if a.status == mt.LIVE:
            out.update(int(x) for x in
                       (((a.changes or {}).get("applied") or {}).get("clauses_deleted") or []))
    return out


def deleted_clause_ids(s, contract_id: int) -> set[int]:
    """_deleted_clause_ids for a contract id — for Bordereau Setup, which must
    not make rules from a clause a change deleted."""
    c = s.get(Contract, contract_id)
    return _deleted_clause_ids(s, c) if c is not None else set()


def _clause_rows(s, c: Contract) -> list[dict]:
    return [dict(r) for r in s.execute(text(
        "SELECT clause_id, clause_type, title, text, section_header, page_number, "
        "created_by FROM clauses_extracted WHERE contract_id = :c ORDER BY clause_id"),
        {"c": c.id}).mappings().all()]


def _rule_counts(rules: list[dict], day: Optional[dt.date]) -> dict[int, int]:
    """{clause id: checks written from it that are in force on the day}."""
    out: dict[int, int] = {}
    for r in rules:
        cid = r.get("source_clause_id")
        if cid and r.get("rule_status") in ("active", "disabled", "needs_review") \
                and mt.in_force(r, day or cr._today()):
            out[int(cid)] = out.get(int(cid), 0) + 1
    return out


def _section_clause_ids(s, c: Contract, sections: list[dict],
                        rows: list[dict]) -> dict[str, Optional[int]]:
    """{section key: the clauses_extracted row that mirrors it}. A written
    contract's sections are stored as clause rows too (contract_routes.
    _store_authored_clauses), and its checks name those rows. Matched by the
    id a change wrote on the section, then by the "N. Title" header the rows
    are stored under, then by position — never across contracts."""
    mine = [r for r in rows if r.get("created_by") in ("contract_wording", mt.CREATED_BY)]
    ids = {r["clause_id"] for r in mine}
    by_header = {(r.get("section_header") or ""): r["clause_id"] for r in mine}
    out: dict[str, Optional[int]] = {}
    used: set = set()
    for i, sec in enumerate(sections, start=1):
        cid = sec.get("clause_id")
        if cid not in ids:
            cid = by_header.get(f"{i}. {sec.get('title') or 'Section'}")
        if cid is None and i - 1 < len(mine):
            cid = mine[i - 1]["clause_id"]
        if cid in used:
            cid = None
        if cid is not None:
            used.add(cid)
        out[sec.get("key")] = cid
    return out


def _current_clauses(s, c: Contract, rules: Optional[list[dict]] = None,
                     day: Optional[dt.date] = None) -> list[dict]:
    """The clauses a change starts from: {ref, clause_id, title, section, text,
    body?, page, checks}. An uploaded contract's are the rows read out of its
    PDF (ref "c:<clause id>"); a written contract's are its wording sections
    (ref "s:<section key>"), whose text is the section with its terms filled
    in, and whose `body` keeps the terms as tokens so they follow the terms."""
    import contract_wording as cw
    rules = _rules_of(s, c) if rules is None else rules
    counts = _rule_counts(rules, day)
    rows = _clause_rows(s, c)
    if cr._is_uploaded(c):
        gone = _deleted_clause_ids(s, c)
        return [{"ref": f"c:{r['clause_id']}", "clause_id": r["clause_id"],
                 "title": r.get("title"), "section": r.get("section_header"),
                 "text": r.get("text") or "", "page": r.get("page_number"),
                 "kind": r.get("clause_type"), "checks": counts.get(r["clause_id"], 0)}
                for r in rows if r["clause_id"] not in gone]
    sections = cr._composable(cr._contract_sections(c))
    tokens = cr._wording_context(s, c)["tokens"]
    mirror = _section_clause_ids(s, c, sections, rows)
    out = []
    for sec in sections:
        cid = mirror.get(sec.get("key"))
        out.append({"ref": f"s:{sec.get('key')}", "clause_id": cid,
                    "title": sec.get("title"), "section": sec.get("title"),
                    "text": cw.render(sec.get("body") or "", tokens).strip(),
                    "body": sec.get("body") or "", "origin": sec.get("origin"),
                    "page": None, "kind": None,
                    "checks": counts.get(cid, 0) if cid else 0})
    return out


def _rule_column(r: dict) -> Optional[str]:
    """The column a check (a stored rule, a planned one, or a generated one)
    measures — what tells two checks of one column apart from two of two."""
    target = r.get("canonical_target") or {}
    if isinstance(target, str):
        try:
            target = json.loads(target)
        except ValueError:
            target = {}
    if target.get("output_field"):
        return target["output_field"]
    spec = r.get("rule_spec") or {}
    for p in ((spec.get("params") or {}), ((spec.get("ir") or {}).get("params") or {})):
        if p.get("field"):
            return p["field"]
    return None


def _clause_inputs(s, c: Contract, body: MidTermIn, current: list[dict], old: dict,
                   new: dict, term_changes: list[dict], uploaded: bool
                   ) -> tuple[list[dict], dict]:
    """(clause changes, {field: why}) — the carrier's clause edits against the
    clauses as they read today.

    A written contract's sections are edited with their terms as tokens, so
    they are compared as tokens and quoted in the change document as words,
    each side with its own terms. For an uploaded contract, a term the change
    moves re-words the clause it was read from as well ("8) Commissions
    Schedule: 23.5%" → "… 25.5%") — text only, `auto`: the term's own check
    moves with the term, so the clause's checks are not worked out again."""
    import contract_wording as cw
    if uploaded:
        changes, errors = mt.clause_changes(current, body.clauses or [])
        edited = {ch["ref"] for ch in changes if ch.get("ref")}
        by_id = {x["clause_id"]: x for x in current if x.get("clause_id")}
        for w in term_changes:
            if w["kind"] != "amended":
                continue
            o = old.get(w["key"]) or {}
            cl = by_id.get(o.get("clause_id"))
            if cl is None or cl["ref"] in edited:
                continue
            reworded = mt.swap_value_in_clause(cl["text"], o.get("value"),
                                               (new.get(w["key"]) or {}).get("value"))
            if reworded:
                changes.append({"index": None, "ref": cl["ref"], "clause_id": cl["clause_id"],
                                "kind": cl.get("kind"), "section": cl.get("section"),
                                "from_title": cl.get("title"), "from_text": cl["text"],
                                "action": mt.CLAUSE_AMENDED, "title": cl.get("title"),
                                "to_text": reworded, "auto": True, "term": w["key"]})
                edited.add(cl["ref"])
        return changes, errors
    changes, errors = mt.clause_changes(
        [{**x, "text": x.get("body") or ""} for x in current], body.clauses or [])
    ctx = cr._wording_context(s, c)
    new_tokens = cw.token_values(values=ctx["values"], limits=new,
                                 carrier_name=ctx["carrier_name"],
                                 counterparty_name=ctx["counterparty_name"],
                                 programme_name=ctx["programme_name"])
    for ch in changes:
        ch["body"] = ch.get("to_text")
        if ch.get("to_text") is not None:
            ch["to_text"] = cw.render(ch["to_text"], new_tokens).strip()
        if ch.get("from_text") is not None:
            ch["from_text"] = cw.render(ch["from_text"], ctx["tokens"]).strip()
    return changes, errors


_NEW_CLAUSE_BASE = 1_000_000


def _clause_key(ch: dict) -> int:
    """A clause change's place in the generated checks: its clause id, or for a
    new clause (no id yet) a stand-in from its place in the request — far from
    the generic rule library's own negative ids, which Pipeline 2 also uses."""
    return (int(ch["clause_id"]) if ch.get("clause_id")
            else -(_NEW_CLAUSE_BASE + int(ch["index"])))


def _clause_rule_plan(c: Contract, changes: list[dict], rules: list[dict],
                      fields_by_set: dict, day: Optional[dt.date], term_plan: dict,
                      *, work_out: bool, stored: Optional[dict]) -> dict:
    """Which checks the re-worded, added and deleted clauses close and open.

    CLOSED: every check written from a re-worded or deleted clause that is in
    force on the change date — on and off alike — except one the term plan
    already closes. OPENED: what Bordereau Setup's own Pipeline 2 makes of the
    new words, per rule set, dated from the change date; a generated check on
    a column the term plan already opens a version for is left out (the term's
    own check carries it). The checks are generated on Send, or in the preview
    when the carrier asks; stored on the change, and written at go-live
    exactly as they were shown (`stored`) — never asked for again.

    state: none (no clause change) · ready · not_worked_out · failed ·
    no_setup (the contract has no Bordereau Setup: nothing to close, nothing
    to bind new words to — its setup reads the new wording when it is made)."""
    out: dict = {"close": [], "open": [], "state": "none", "error": None, "generated": {},
                 "no_column": [], "carried_terms": []}
    if not changes or not day:
        return out
    taken = {x["rule_id"] for x in term_plan["close"]}
    live = [r for r in rules if mt.in_force(r, day)
            and r.get("rule_status") in ("active", "disabled", "needs_review")]

    # A clause re-worded only because its term moved is carried by the term's
    # own check — when the term plan found one. When it found none (no column
    # of the setup is known to measure the term) but checks WERE written from
    # that clause, those checks are the term's: left alone they went on
    # checking the old value after the change went live. Such a clause is
    # handled like one the carrier re-worded — its checks closed, new ones made
    # from its new words. At go-live only what Send worked out is written, so
    # a change sent before this existed (nothing worked out for the clause)
    # keeps what was agreed rather than closing checks with nothing after them.
    unchecked = {x.get("key") for x in term_plan.get("unchecked") or []}

    def carries(ch) -> bool:
        if not (ch.get("auto") and ch.get("term") in unchecked and ch.get("clause_id")):
            return False
        if not any(r.get("source_clause_id") == ch["clause_id"] and r["rule_id"] not in taken
                   for r in live):
            return False
        return stored is None or any(_clause_key(ch) in ((v or {}).get("worked") or [])
                                     for v in stored.values())

    # Decided once, before any check is taken below.
    carried_keys = {_clause_key(ch) for ch in changes if carries(ch)}

    def carried(ch) -> bool:
        return bool(ch.get("auto")) and _clause_key(ch) in carried_keys

    for ch in changes:
        if (ch.get("auto") and not carried(ch)) or ch["action"] == mt.CLAUSE_ADDED \
                or not ch.get("clause_id"):
            continue
        for r in live:
            if r.get("source_clause_id") == ch["clause_id"] and r["rule_id"] not in taken:
                out["close"].append({
                    "rule_id": r["rule_id"], "rule_name": r.get("rule_name"), "key": None,
                    "set": r.get("output_template_id"), "fingerprint": mt.rule_fingerprint(r),
                    "from": None, "clause": ch.get("from_title") or ch.get("title"),
                    "clause_ref": ch.get("ref")})
                taken.add(r["rule_id"])
    worded = [ch for ch in changes if (not ch.get("auto") or carried(ch)) and ch.get("to_text")
              and ch["action"] in (mt.CLAUSE_AMENDED, mt.CLAUSE_ADDED)]
    out["carried_terms"] = sorted({ch["term"] for ch in worded if ch.get("auto")})
    out["state"] = "ready"
    if not worded:
        return out
    # A written contract's wording rows are bound to its own template only (as
    # app_routes._add_template_rule_set leaves them out of added sets).
    sets = [sid for sid, f in fields_by_set.items()
            if f and (sid is None or cr._is_uploaded(c))]
    if not sets:
        out["state"] = "no_setup"
        return out
    term_cols = {(x.get("set"), _rule_column(x)) for x in term_plan["open"]}
    rows = [{"clause_id": _clause_key(ch), "clause_type": ch.get("kind"),
             "title": ch.get("title"), "text": ch["to_text"],
             "section_header": ch.get("section")} for ch in worded]
    for sid in sets:
        skey = "own" if sid is None else str(sid)
        if stored is not None:
            kept = stored.get(skey) or {}
            by_clause = {int(k): v for k, v in (kept.get("rules") or {}).items()}
            unbound = {int(k): v for k, v in (kept.get("no_column") or {}).items()}
        elif work_out:
            by_clause, unbound, err = mta.rules_for_clauses(
                rows, fields_by_set[sid], contract_id=c.id, tenant_id=c.tenant_id,
                program_id=c.program_id)
            if err:
                out.update(state="failed", error=err, open=[])
                return out
            by_clause = json.loads(json.dumps(by_clause, default=str))
            by_clause = {int(k): v for k, v in by_clause.items()}
        else:
            out.update(state="not_worked_out", open=[])
            return out
        out["generated"][skey] = {"rules": {str(k): v for k, v in by_clause.items()},
                                  "no_column": {str(k): v for k, v in unbound.items()},
                                  "worked": [_clause_key(ch) for ch in worded]}
        for ch in worded:
            why = unbound.get(_clause_key(ch))
            if why:
                out["no_column"].append({"set": sid, "clause": ch.get("title"),
                                         "clause_ref": ch.get("ref"),
                                         "clause_index": ch.get("index"), "reason": why})
        for ch in worded:
            for r in by_clause.get(_clause_key(ch)) or []:
                col = _rule_column(r)
                if (sid, col) in term_cols:
                    continue
                spec = r.get("rule_spec") or {}
                out["open"].append({
                    "replaces": None, "key": None, "set": sid,
                    "clause_key": _clause_key(ch), "clause_ref": ch.get("ref"),
                    "clause_index": ch.get("index"), "clause": ch.get("title"),
                    "rule_name": r.get("rule_name"),
                    "rule_status": r.get("rule_status") or "active",
                    "severity": r.get("severity"), "column": col,
                    "executable": spec.get("executable", True) is not False,
                    "error_message": r.get("error_message"), "rule": r})
    return out


# =============================================================================
#  The plan — what the preview shows and the send stores
# =============================================================================

def _plan(s, c: Contract, body: MidTermIn,
          amendment: Optional[ContractAmendment] = None, *, read: str = "cached",
          work_out: bool = False, stored_clause_rules: Optional[dict] = None) -> dict:
    """What the change says and does. `read` — see _old_terms. `work_out` asks
    the model for the checks re-worded and new clauses make; at go-live
    `stored_clause_rules` (what Send stored) is used instead, so the checks
    written are the ones that were agreed."""
    import contract_rules as crr
    import contract_wording as cw

    blockers: list[dict] = []

    def block(code, message, field=None):
        blockers.append({"code": code, "message": message,
                         **({"field": field} if field else {})})

    try:
        day = cr._as_date(body.effective_from) if body.effective_from else None
    except (TypeError, ValueError):
        day = None
        block("effective_from", "Write the date as YYYY-MM-DD.", "effective_from")

    lifecycle = cr._effective_lifecycle(c)
    if lifecycle not in ("active", "expired"):
        block("state", f"This contract is {lifecycle}. A mid-term change is made to a "
                       f"contract that is running (active or expired).")
    # A change works by DATING checks: each one it moves is closed the day
    # before and its new version opened on the day. A contract never set up in
    # Bordereau Setup has none, and setting it up after the change was live
    # would check rows from before the change on the new wording. So it is set
    # up first (contract_checks; its contract page offers both ways).
    import contract_checks as _cc
    _serving = _cc.serving_setup(s, c)
    if _serving is None or not _cc.check_count(s, _serving, c.id):
        block("no_checks", NO_CHECKS_WORDS)
    other_open = _open_amendment(s, c)
    if other_open is not None and (amendment is None or other_open.id != amendment.id):
        block("open", f"Mid-term change {other_open.number} is still in progress. Finish "
                      f"or withdraw it first — one change at a time.")

    latest = _latest_live_from(s, c)
    for f, why in mt.effective_date_errors(day, c.inception_dt, c.expiry_dt,
                                           latest).items():
        block("effective_from", why, f)

    # The terms a change starts from are the ones in force when it starts: on
    # its own date, or — before a date is picked — after the last live change,
    # which may itself start after today. Read as of today, a change made
    # ahead of an earlier one's date opened with the older values, and sending
    # them unchanged quietly put those back from its own date.
    as_of = day or max(cr._today(), latest or cr._today())
    rules = _rules_of(s, c)
    fields_by_set, tids = _fields_by_set(s, c, rules)
    old, terms_error = _old_terms(s, c, rules, fields_by_set.get(None) or [], as_of, read)
    new = ct.clean_agreed_limits(body.agreed_limits or {})
    old_plain = _without_marks(old)
    wording_changes = cw.derive_endorsement_changes(old_plain, new)
    uploaded = cr._is_uploaded(c)
    current_clauses = _current_clauses(s, c, rules, as_of)
    clause_list, clause_errors = _clause_inputs(s, c, body, current_clauses, old, new,
                                                wording_changes, uploaded)
    for field, why in clause_errors.items():
        block("clauses", why, field)
    if not wording_changes and not clause_list:
        block("no_change", "Nothing has changed yet. Change a term or a clause to make "
                           "a mid-term change.")
    raw = [{**w, "from": (old_plain.get(w["key"]) or {}).get("value"),
            "to": (new.get(w["key"]) or {}).get("value")} for w in wording_changes]

    plan = {"close": [], "open": [], "unchecked": [], "needs_choice": []}
    if day and wording_changes:
        own_tid = tids.get(None)

        def build_new(key, entry, set_id):
            tid = tids.get(set_id)
            built, _unmapped = crr.map_limits_to_template(
                {key: entry}, fields_by_set.get(set_id) or [])
            ok = [b for b in built if b.get("executable")]
            return _term_rule(ok[0], tid if set_id is not None else own_tid) if ok else None

        plan = mt.plan_rules(
            changes=raw, rules=rules, fields_by_set=fields_by_set, effective=day,
            limits_spec=ct.AGREED_LIMITS, limit_columns=crr.LIMIT_COLUMNS,
            limit_operators=crr.LIMIT_OPERATORS, find_column=crr._find_column,
            build_new=build_new,
            compile_for_set=lambda set_id: _compiler(fields_by_set.get(set_id) or []),
            choices={k: v for k, v in (body.choices or {}).items()})
    for n in plan["needs_choice"]:
        block("needs_choice", f"Kavachio isn't sure which check “{n['question']}” "
                              f"replaces — pick it below.")
    clause_plan = _clause_rule_plan(c, clause_list, rules, fields_by_set, day, plan,
                                    work_out=work_out, stored=stored_clause_rules)
    if clause_plan["state"] == "failed":
        block("clause_checks", "The checks for the changed clauses could not be worked "
                               "out: " + (clause_plan["error"] or "try again."))
    plan = {**plan, "close": plan["close"] + clause_plan["close"]}
    if clause_plan["carried_terms"] and clause_plan["state"] in ("ready", "not_worked_out"):
        # No column measures the term, but its clause's checks are moved with
        # the clause — so it is not left unchecked.
        plan = {**plan, "unchecked": [x for x in plan["unchecked"]
                                      if x.get("key") not in clause_plan["carried_terms"]]}
    if uploaded:
        # A term whose own clause the change re-words is carried by that
        # clause's new checks (made the way the old ones were, on the same
        # columns) — not also by a check built afresh from the term, which
        # would measure the same thing twice. A version of an existing check
        # (the term plan found it) stays: it IS that check, moved.
        reworded = {ch["clause_id"] for ch in clause_list if not ch.get("auto")
                    and ch["action"] == mt.CLAUSE_AMENDED and ch.get("clause_id")}
        carried = {k for k, v in old.items() if (v or {}).get("clause_id") in reworded}
        if carried and clause_plan["state"] in ("ready", "not_worked_out"):
            plan = {**plan, "open": [x for x in plan["open"]
                                     if not (x.get("replaces") is None and x.get("key") in carried)],
                    "unchecked": [x for x in plan["unchecked"] if x.get("key") not in carried]}
    anything = bool(wording_changes or clause_list)

    # Bordereau Setup: every setup that runs this contract must say which of
    # its columns dates a row — a dated check can't be placed without it.
    setups = []
    for pp in _setups(s, c):
        structure = _structure(s, pp.output_template_id)
        cols = []
        for sh in structure.get("sheets") or []:
            for col in sh.get("columns") or []:
                name = col.get("column_name")
                if name and name not in cols:
                    cols.append(name)
        chosen = (str((body.date_columns or {}).get(str(pp.id))
                      or (body.date_columns or {}).get(pp.id) or "").strip()
                  or (pp.governing_date_field or "").strip()
                  or rw.date_column_from_structure(structure) or None)
        setups.append({
            "pipeline_id": pp.id, "name": pp.name or f"Setup {pp.id}",
            "status": getattr(pp, "status", None),
            "template_id": pp.output_template_id,
            "date_column": chosen,
            "saved": (pp.governing_date_field or None),
            "suggested": rw.date_column_from_structure(structure),
            "columns": cols,
        })
        if anything and not chosen:
            block("date_column", f"Choose the transaction effective date column for "
                                 f"“{pp.name or 'Setup ' + str(pp.id)}”.")
        elif chosen and cols and chosen not in cols:
            block("date_column", f"“{chosen}” is not a column of "
                                 f"“{pp.name or 'Setup ' + str(pp.id)}”.")

    if not (body.reason or "").strip():
        block("reason", "Give the reason for the change — the broker will see it.",
              "reason")

    number = int(amendment.number) if amendment is not None else _next_number(s, c)
    eff_words = mt.fmt_date(day) if day else None
    # An uploaded change document IS the document; Kavachio composes none.
    document = _uploaded_document(s, c, body.document_id, amendment)
    if body.document_id and document is None:
        block("document", "That change document is not one uploaded to this contract.",
              "document_id")
    if body.signed and document is None:
        block("document", "Upload the signed change document first.", "document_id")
    sections = [sec for sec in (body.sections or [])
                if isinstance(sec, dict) and (sec.get("body") or "").strip()]
    if not sections and anything and document is None:
        # A clause re-worded only because its term moved is said once, by the
        # term's own line.
        sections = cw.build_endorsement_sections(
            contract_name=_plain_name(c), number=number,
            effective_from=eff_words, changes=wording_changes, note=body.reason,
            clause_changes=[x for x in clause_list if not x.get("auto")])
    if document is not None:
        sections = []

    in_force = [r for r in rules if mt.in_force(r, day) and r.get("rule_status") == "active"] \
        if day else []
    touched = {x["rule_id"] for x in plan["close"]}
    road = (mt.ROAD_UPLOADED_SIGNED if (document is not None and body.signed) else
            mt.ROAD_UPLOADED_TYPED if uploaded else mt.ROAD_AUTHORED)
    return {
        "number": number, "effective_from": str(day) if day else None,
        "boundary": mt.boundary_words(day) if day else None,
        "road": road,
        "is_uploaded": uploaded,
        "current_terms": old, "new_terms": new,
        "terms_error": terms_error,
        "changes": wording_changes,
        "clause_changes": clause_list,
        "clause_rules": clause_plan,
        "document": ({"id": document.id, "filename": document.filename}
                     if document is not None else None),
        "signed": bool(body.signed and document is not None),
        "rules": plan,
        "checks_unchanged": len([r for r in in_force if r["rule_id"] not in touched]),
        "setups": setups,
        "recheck": _recheck_files(s, c, day),
        "sections": sections,
        "blockers": blockers,
        "ok": not blockers,
        "_day": day, "_raw": raw, "_tids": tids,
    }


def _public_clause(ch: dict) -> dict:
    return {k: ch.get(k) for k in ("index", "ref", "clause_id", "action", "title",
                                   "from_title", "section", "from_text", "to_text",
                                   "auto", "term")}


def _public_plan(plan: dict) -> dict:
    """The plan as the screen reads it: each check named, with the days each
    version covers, and nothing the screen has no use for (compiled SQL, the
    generated rules in full)."""
    day = plan.get("_day")
    until = mt.fmt_date(day - dt.timedelta(days=1)) if day else None
    since = mt.fmt_date(day) if day else None
    r = plan["rules"]
    cp = plan.get("clause_rules") or {}
    return {
        **{k: v for k, v in plan.items() if not k.startswith("_")
           and k not in ("rules", "clause_rules", "clause_changes")},
        "clause_changes": [_public_clause(ch) for ch in plan.get("clause_changes") or []],
        "clause_checks": {
            "state": cp.get("state"), "error": cp.get("error"),
            "no_column": [{k: x.get(k) for k in ("clause", "clause_ref", "clause_index", "reason")}
                          for x in cp.get("no_column") or []],
            "opening": [{"clause": x.get("clause"), "clause_ref": x.get("clause_ref"),
                         "clause_index": x.get("clause_index"),
                         "rule_name": x.get("rule_name"), "column": x.get("column"),
                         "severity": x.get("severity"), "from": since,
                         "executable": x.get("executable"),
                         "switched_off": x.get("rule_status") == "disabled",
                         "needs_review": x.get("rule_status") == "needs_review",
                         "message": x.get("error_message")}
                        for x in cp.get("open") or []]},
        "rules": {
            "closing": [{"rule_id": x["rule_id"], "rule_name": x.get("rule_name"),
                         "key": x["key"], "value": x.get("from"), "until": until,
                         "clause": x.get("clause")}
                        for x in r["close"]],
            "opening": [{"replaces": x.get("replaces"), "rule_name": x.get("rule_name"),
                         "key": x["key"], "value": x.get("to"), "from": since,
                         "new_check": x.get("replaces") is None,
                         "switched_off": x.get("rule_status") == "disabled",
                         "message": x.get("error_message")}
                        for x in r["open"]],
            "unchecked": r["unchecked"], "needs_choice": r["needs_choice"],
        },
    }


def _raise_blocked(plan: dict) -> None:
    if plan["blockers"]:
        first = plan["blockers"][0]
        raise HTTPException(400, {
            "message": first["message"],
            "errors": {b.get("field") or b["code"]: b["message"] for b in plan["blockers"]},
            "blockers": plan["blockers"]})


# =============================================================================
#  The change document
# =============================================================================

def _compose_document(s, c: Contract, a: ContractAmendment, plan: dict,
                      p: Principal) -> ContractDocument:
    """Composed once and stored (contract_wording.compose_endorsement_pdf says
    why). A revised change gets a new document and the old one is retired."""
    import contract_wording as cw
    import storage
    from db import Tenant
    for d in (s.query(ContractDocument)
              .filter(ContractDocument.contract_id == c.id,
                      ContractDocument.amendment_id == a.id,
                      ContractDocument.is_active.is_(True)).all()):
        d.is_active = False
    party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
    carrier = s.get(Tenant, c.tenant_id) if c.tenant_id else None
    data = cw.compose_endorsement_pdf(
        contract_name=_plain_name(c), number=int(a.number),
        carrier_name=(getattr(carrier, "legal_name", None)
                      or getattr(carrier, "tenant_name", None)),
        counterparty_name=getattr(party, "legal_name", None),
        effective_from=mt.fmt_date(a.effective_date), sections=plan["sections"])
    revision = (s.query(ContractDocument)
                .filter(ContractDocument.amendment_id == a.id).count())
    filename = (f"Mid-term change {a.number}"
                + (f" (revised {revision})" if revision else "")
                + f" — {_plain_name(c)}.pdf")
    blob_ref, blob = storage.store_or_keep(
        "contract-documents", c.tenant_id, filename, data, "application/pdf")
    doc = ContractDocument(
        tenant_id=c.tenant_id, contract_id=c.id, kind="endorsement",
        filename=filename, version=f"mid-term-{a.number}" + (f"-r{revision}" if revision else ""),
        blob_ref=blob_ref, blob=blob, fingerprint=hashlib.sha256(data).hexdigest(),
        effective_from=a.effective_date, is_active=True, uploaded_by_user_id=p.user_id,
        amendment_id=a.id,
        extracted={"changes": plan["changes"], "note": a.description,
                   "endorsement_number": int(a.number), "mid_term": True})
    s.add(doc)
    s.flush()
    return doc


def _plain_name(c: Contract) -> str:
    """The contract's name for a document title — an uploaded contract is named
    after its file, and "….pdf.pdf" helps nobody."""
    import os
    name = (c.name or c.filename or f"Contract {c.id}").strip()
    root, ext = os.path.splitext(name)
    return root if ext.lower() in (".pdf", ".docx", ".doc") else name


def _document(s, a: ContractAmendment) -> Optional[ContractDocument]:
    return (s.query(ContractDocument)
            .filter(ContractDocument.amendment_id == a.id,
                    ContractDocument.is_active.is_(True))
            .order_by(ContractDocument.id.desc()).first())


def _uploaded_document(s, c: Contract, document_id: Optional[int],
                       amendment: Optional[ContractAmendment] = None
                       ) -> Optional[ContractDocument]:
    """A change document the carrier uploaded for THIS contract — not yet
    attached to a change, or attached to the one being revised."""
    if not document_id:
        return None
    d = s.get(ContractDocument, int(document_id))
    if d is None or d.contract_id != c.id or d.kind != "endorsement":
        return None
    if not ((d.extracted or {}) if isinstance(d.extracted, dict) else {}).get("mid_term_upload"):
        return None
    if d.amendment_id is not None and (amendment is None or d.amendment_id != amendment.id):
        return None
    return d


def _attach_document(s, c: Contract, a: ContractAmendment, d: ContractDocument) -> ContractDocument:
    """The uploaded change document becomes this change's document; any
    other (an earlier upload, or a composed one) is retired."""
    for old in (s.query(ContractDocument)
                .filter(ContractDocument.amendment_id == a.id,
                        ContractDocument.id != d.id,
                        ContractDocument.is_active.is_(True)).all()):
        old.is_active = False
    d.amendment_id = a.id
    d.is_active = True
    d.effective_from = a.effective_date
    d.version = f"mid-term-{a.number}"
    return d


def _setup_follow_up(s, c: Contract, a: ContractAmendment) -> Optional[dict]:
    """After a change goes live: does each Bordereau Setup that runs this
    contract already feed the checks the change ADDED?

    A new check reads an output column. Three answers, per setup:
      * ready     — the column is in the setup's output template and its
                    mapping fills it: the next file is checked, nothing to do;
      * unmapped  — the column is in the template but the mapping fills it
                    from nothing: map it, with the SAME input and output
                    templates (the setup's own edit page);
      * no_column — the template has no such column (the check was saved
                    not executable, or names a column the template lacks):
                    the setup needs a new output template, and a sample with
                    that column.
    With no setup at all, the contract's new wording is read when it is set up.
    Worked out live from the setup as it is now, so the card clears itself once
    the mapping is done."""
    import validation_outcome as vo
    from contract_upload_services.regen_reconcile import target_fields
    from db import DirectFormat
    applied = (a.changes or {}).get("applied") or {}
    ids = [int(x) for x in applied.get("new_checks") or []]
    setups = _setups(s, c)
    link = (f"/direct/setup?program_id={c.program_id}"
            + (f"&broker_party_id={c.broker_party_id}" if c.broker_party_id else ""))
    changed_words = bool([x for x in (a.changes or {}).get("clauses") or [] if not x.get("auto")]
                         or (a.changes or {}).get("wording"))
    if not setups:
        return ({"no_setup": True, "recommended": "new_templates", "setups": [],
                 "checks": [], "new_checks": 0, "all_ready": False, "setup_link": link}
                if changed_words else None)
    nocol = applied.get("clauses_without_column") or []
    if not ids and not nocol:
        return None
    rows = s.execute(text(
        "SELECT rule_id, rule_name, rule_status, rule_spec, canonical_target, "
        "output_template_id FROM validation_rule WHERE rule_id = ANY(:ids) "
        "ORDER BY rule_id"), {"ids": ids}).mappings().all()
    checks = []
    for r in rows:
        d = dict(r)
        for k in ("rule_spec", "canonical_target"):
            if isinstance(d.get(k), str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    d[k] = {}
        spec = d.get("rule_spec") or {}
        cols = list(target_fields(d)) or [x for x in [_rule_column(d)] if x]
        checks.append({"rule_id": d["rule_id"], "rule_name": d.get("rule_name"),
                       "columns": cols, "set": d.get("output_template_id"),
                       "executable": spec.get("executable", True) is not False,
                       "unmapped_reason": spec.get("unmapped_reason")})
    out_setups = []
    for pp in setups:
        structure = _structure(s, pp.output_template_id)
        template_cols = {col.get("column_name") for sh in structure.get("sheets") or []
                         for col in sh.get("columns") or [] if col.get("column_name")}
        fmt = s.get(DirectFormat, pp.input_format_id) if pp.input_format_id else None
        filled = {col for cols in vo.filled_cols_from_mapping(
            getattr(fmt, "column_mapping", None) or {}).values() for col in cols}
        mine = []
        for ch in checks:
            if ch["set"] is not None and ch["set"] != pp.output_template_id:
                continue
            missing = [col for col in ch["columns"] if col not in template_cols]
            unfilled = [col for col in ch["columns"] if col in template_cols and col not in filled]
            state = ("no_column" if (not ch["executable"] or missing or not ch["columns"]) else
                     "unmapped" if unfilled else "ready")
            mine.append({**ch, "state": state, "missing": missing, "unfilled": unfilled})
        out_setups.append({
            "pipeline_id": pp.id, "name": pp.name or f"Setup {pp.id}",
            "status": getattr(pp, "status", None), "checks": mine,
            "unmapped": sorted({col for x in mine for col in x["unfilled"]}),
            "no_column": sorted({col for x in mine for col in x["missing"]}
                                | {x["rule_name"] for x in mine
                                   if x["state"] == "no_column" and not x["missing"]}),
            "edit_link": f"/direct/setups/{pp.id}/edit",
        })
    states = [x["state"] for st in out_setups for x in st["checks"]]
    return {"no_setup": False, "new_checks": len(checks), "checks": checks,
            "setups": out_setups,
            # words the template has no column for: no check could be made
            "clauses_without_column": nocol,
            "all_ready": bool(states) and all(x == "ready" for x in states) and not nocol,
            # The same templates do the job unless the change needs a column the
            # output template does not have.
            "recommended": ("new_templates" if ("no_column" in states or nocol)
                            else "same_templates"),
            "setup_link": link}


# =============================================================================
#  History, audit and email
# =============================================================================

def _approval(s, c: Contract, a: ContractAmendment, p: Principal, step: str,
              note: Optional[str] = None, extra: Optional[dict] = None) -> None:
    s.add(ContractApproval(
        tenant_id=c.tenant_id, contract_id=c.id, action=mt.APPROVAL[step],
        acted_by_user_id=p.user_id, acted_at=dt.datetime.now(dt.timezone.utc),
        note=note, proposed_changes={"amendment_id": a.id, "number": int(a.number),
                                     **(extra or {})}))


def _activity(s, c: Contract, a: ContractAmendment, p: Principal, step: str,
              details: Optional[dict] = None) -> None:
    """The audit row, in the SAME transaction as the step it records (as
    Change period does), so a change can never be recorded as sent, agreed,
    signed or live without having become so. Carries what the bell and the
    Audit Logs screen name: the contract, its programme and its broker."""
    from db import ActivityEvent
    from auth_deps import normalize_role
    prog = s.get(Program, c.program_id) if c.program_id else None
    party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
    s.add(ActivityEvent(
        tenant_id=c.tenant_id, actor=cr._principal_email_for(s, p),
        action=mt.ACTIVITY[step], target=f"contract:{c.id}",
        details={"contract_id": c.id, "name": c.name or c.filename or f"Contract {c.id}",
                 "program_id": c.program_id, "program_name": getattr(prog, "name", None),
                 "broker_name": getattr(party, "legal_name", None),
                 "amendment_id": a.id, "number": int(a.number),
                 "effective_from": str(a.effective_date) if a.effective_date else None,
                 "status": a.status, **(details or {}),
                 "acted_at_utc": dt.datetime.now(dt.timezone.utc).isoformat()},
        actor_user_id=p.user_id, actor_role=normalize_role(p.role),
        actor_broker_party_id=(c.broker_party_id if p.is_broker else None)))


def _change_lines(a: ContractAmendment) -> list[str]:
    out = []
    for ch in ((a.changes or {}).get("wording") or []):
        if ch.get("kind") == "amended":
            out.append(f"{ch['question']}: {ch['from']} → {ch['to']}")
        elif ch.get("kind") == "added":
            out.append(f"{ch['question']}: {ch['to']} (new)")
        else:
            out.append(f"{ch['question']}: {ch['from']} no longer applies")
    for ch in ((a.changes or {}).get("clauses") or []):
        if not ch.get("auto"):
            out.append(mt.clause_change_line(ch))
    return out


def _broker_email_thunks(s, c: Contract, a: ContractAmendment, kind: str) -> list:
    """D7: the broker is emailed when a change is sent and when it goes live —
    the broker's own notification bell is off by design. Only the broker's
    admins, and only while the broker is still on the programme."""
    from db import Tenant
    contacts = cr._period_broker_contacts(s, c)
    if not contacts:
        return []
    prog = s.get(Program, c.program_id) if c.program_id else None
    carrier = s.get(Tenant, c.tenant_id) if c.tenant_id else None
    carrier_name = ((carrier.legal_name or carrier.tenant_name) if carrier else None) \
        or "Your carrier"
    program_name = (prog.name if prog else None) or "your programme"
    code = cr._codes().contract_code(c)
    name = c.name or c.filename or f"Contract {c.id}"
    lines = _change_lines(a)
    effective = mt.fmt_date(a.effective_date)
    from notifications import _app_link
    # Opens the contract on the change itself (MidTermCard), not its top.
    link = _app_link(f"/contracts/{c.id}?show=mid-term")

    def _sender(person: dict):
        def send() -> None:
            from email_utils import send_email, mid_term_email_html
            subject = (f"{carrier_name} made mid-term change {a.number} to your contract "
                       f"on {program_name} — it needs your agreement" if kind == "sent" else
                       f"Mid-term change {a.number} to your contract on {program_name} "
                       f"is now live")
            send_email(
                person["email"], subject,
                mid_term_email_html(person.get("name"), carrier_name, program_name, name,
                                    code, int(a.number), effective, lines,
                                    a.description or "", link, live=(kind == "live")),
                text=(f"{subject}. Contract {code}. From {effective}: "
                      + "; ".join(lines) + f". Reason: {a.description or '—'}. {link}"),
                account="NOTIFY")
        return send
    return [(x["email"], _sender(x)) for x in contacts if x.get("email")]


def _notify_carrier(s, c: Contract, a: ContractAmendment, p: Principal,
                    title: str, body: str, now: str) -> None:
    """The broker answered: mail the carrier admins, whose move it now is."""
    try:
        from notifications import (CARRIER_ADMIN_FOOTER, carrier_admin_recipients,
                                   notify_people)
        prog = s.get(Program, c.program_id) if c.program_id else None
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        notify_people(
            carrier_admin_recipients(c.tenant_id), title, body=body,
            facts=[f for f in (
                ("Contract", c.name or c.filename or f"Contract {c.id}"),
                ("Mid-term change", f"{a.number}, from {mt.fmt_date(a.effective_date)}"),
                ("With", getattr(party, "legal_name", None)),
                ("Programme", getattr(prog, "name", None)),
                ("Now", now)) if f[1]],
            link_path=f"/contracts/{c.id}", link_label="Open the contract",
            action="Open it", subject=f"{title}: {c.name or c.filename}",
            footer=CARRIER_ADMIN_FOOTER)
    except Exception:  # noqa: BLE001
        log.exception("mid-term change %s: carrier notice not sent", a.id)


# =============================================================================
#  Going live
# =============================================================================

def _insert_rule(s, c: Contract, a: ContractAmendment, entry: dict,
                 day: dt.date, source_text: str) -> int:
    """The new version of a check, from the change's effective date. A version
    of an existing check is a copy of that row with the moved value; a check
    for a term that had none is written the way contract_rules writes one."""
    import rule_scope
    spec = json.dumps(entry["rule_spec"])
    common = {"from": day, "aid": a.id, "spec": spec, "src": source_text,
              "msg": entry.get("error_message"), "desc": entry.get("rule_description")}
    if entry.get("replaces"):
        cols = [r[0] for r in s.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'validation_rule' AND table_schema = current_schema()")).all()]
        override = {
            "rule_spec": "CAST(:spec AS JSONB)",
            "error_message": "COALESCE(:msg, error_message)",
            "rule_description": "COALESCE(:desc, rule_description)",
            "effective_from": ":from",
            "effective_to": "NULL", "source_amendment_id": ":aid",
            "source_verbatim_text": ":src", "source_page_number": "NULL",
            "created_by": f"'{mt.CREATED_BY}'", "generation_confidence": "1.0",
            "reviewed_by_user_id": "NULL", "reviewed_at": "NULL",
        }
        if entry.get("severity"):
            override["severity"] = ":sev"
            common["sev"] = entry["severity"]
        skip = {"rule_id", "created_at", "updated_at", "modified_at"}
        keep = [col for col in cols if col not in skip]
        select = ", ".join(override.get(col, col) for col in keep)
        new_id = s.execute(text(
            f"INSERT INTO validation_rule ({', '.join(keep)}) "
            f"SELECT {select} FROM validation_rule WHERE rule_id = :old "
            "RETURNING rule_id"), {**common, "old": entry["replaces"]}).scalar()
        return int(new_id)
    tag = entry.get("set")
    tag_col = ", output_template_id" if (tag is not None and rule_scope.tag_column_present(s)) else ""
    new_id = s.execute(text(f"""
        INSERT INTO validation_rule
            (tenant_id, contract_id, program_id, rule_engine, rule_name, rule_description,
             validation_stage, severity, canonical_target, rule_spec, error_message,
             generation_confidence, rule_status, created_by, effective_from,
             source_amendment_id, source_verbatim_text{tag_col})
        VALUES (:t, :c, :prog, 'custom', :name, :desc, 'input', :sev2,
                CAST(:target AS JSONB), CAST(:spec AS JSONB), :msg, 1.0, 'active',
                '{mt.CREATED_BY}', :from, :aid, :src{', :tag' if tag_col else ''})
        RETURNING rule_id"""),
        {**common, "t": c.tenant_id, "c": c.id, "prog": c.program_id,
         "name": entry.get("rule_name"), "sev2": entry.get("severity") or "warning",
         "target": json.dumps(entry.get("canonical_target") or {}), "tag": tag}).scalar()
    return int(new_id)


def _apply_uploaded_clauses(s, c: Contract, changes: list[dict]) -> dict:
    """An uploaded contract's clause rows, as the change re-words them. A
    re-worded clause keeps its row (its old checks, now closed, still name it,
    and keep its old words in source_verbatim_text; the change keeps them
    too). A deleted one keeps its row as well — the closed checks need it —
    but is set aside: no rule is made from it again, and the clause list
    leaves it out (_deleted_clause_ids). A new one gets a row."""
    done: dict = {"amended": [], "added": {}, "deleted": []}
    by = mt.CREATED_BY
    for ch in changes:
        if ch["action"] == mt.CLAUSE_AMENDED and ch.get("clause_id"):
            # Re-worded by the carrier: what it bears is not known until its
            # checks are worked out — "pending", as a clause not looked at yet
            # (_open_clause_rules marks it once its new checks are written). A
            # clause re-worded only because its term moved keeps its state:
            # the term's own check moved with it.
            s.execute(text(
                "UPDATE clauses_extracted SET title = :t, text = :x, "
                "rule_generation_status = COALESCE(:st, rule_generation_status), "
                "updated_at = now(), updated_by = :by "
                "WHERE clause_id = :id AND contract_id = :c"),
                {"t": ch.get("title"), "x": ch["to_text"], "by": by,
                 "st": None if ch.get("auto") else "pending",
                 "id": ch["clause_id"], "c": c.id})
            done["amended"].append(int(ch["clause_id"]))
        elif ch["action"] == mt.CLAUSE_REMOVED and ch.get("clause_id"):
            s.execute(text(
                "UPDATE clauses_extracted SET rule_generation_status = 'not_rule_bearing', "
                "updated_at = now(), updated_by = :by WHERE clause_id = :id AND contract_id = :c"),
                {"by": by, "id": ch["clause_id"], "c": c.id})
            done["deleted"].append(int(ch["clause_id"]))
        elif ch["action"] == mt.CLAUSE_ADDED:
            new_id = s.execute(text(
                "INSERT INTO clauses_extracted (contract_id, clause_type, title, text, "
                " section_header, extraction_confidence, rule_generation_status, "
                " generated_rule_count, created_by, updated_by) "
                "VALUES (:c, 'other', :t, :x, :h, 1.0, 'pending', 0, :by, :by) "
                "RETURNING clause_id"),
                {"c": c.id, "t": ch.get("title"), "x": ch["to_text"],
                 "h": ch.get("section") or ch.get("title"), "by": by}).scalar()
            done["added"][_clause_key(ch)] = int(new_id)
    return done


def _apply_wording(s, c: Contract, a: ContractAmendment, changes: list[dict],
                   new_terms: dict, removed_terms: list[str]) -> dict:
    """A written contract's wording, as the change leaves it — called after its
    terms (commercial_terms) are set to the new ones.

    Sections re-worded, added ("your own words", as Add a section makes them)
    and deleted (remembered as deleted, with the terms they quoted, so the
    top-up never brings them back — as ContractNew's delete does). A term the
    change removes takes its sentences with it; a term it adds gets its
    sentence where the wording has none (contract_wording.missing_clauses, the
    top-up a PATCH would have run). Then the clause rows that mirror the
    sections are re-worded IN PLACE — their checks name them, so
    _store_authored_clauses' delete-and-insert is not an option here — and a
    new section gets a row of its own."""
    import contract_wording as cw
    raw = c.wording_sections
    ws = dict(raw) if isinstance(raw, dict) else {"sections": list(raw or [])}
    sections = [dict(x) for x in (ws.get("sections") or []) if isinstance(x, dict)]
    rows = _clause_rows(s, c)
    mirror = _section_clause_ids(s, c, cr._composable(sections), rows)
    gone_sections = list(ws.get("dropped_sections") or [])
    gone_terms = list(ws.get("dropped_terms") or [])
    done: dict = {"amended": [], "added": {}, "deleted": []}
    added_keys: dict = {}
    for ch in changes:
        ref = ch.get("ref") or ""
        key = ref[2:] if ref.startswith("s:") else None
        sec = next((x for x in sections if x.get("key") == key), None) if key else None
        if ch["action"] == mt.CLAUSE_AMENDED and sec is not None:
            sec["body"] = ch.get("body") or sec.get("body")
            sec["title"] = ch.get("title") or sec.get("title")
            origin = sec.get("origin") or ""
            if origin and origin != "your own words" and "edited" not in origin:
                sec["origin"] = f"{origin} · edited"
            if mirror.get(key):
                done["amended"].append(int(mirror[key]))
        elif ch["action"] == mt.CLAUSE_REMOVED and sec is not None:
            sections = [x for x in sections if x.get("key") != key]
            gone_sections.append(key)
            gone_terms += [k for k in cw.used_tokens(sec.get("body") or "")
                           if k in ct.AGREED_LIMITS]
            if mirror.get(key):
                done["deleted"].append(int(mirror[key]))
        elif ch["action"] == mt.CLAUSE_ADDED:
            new_key = f"custom_mt{a.id}_{ch.get('index')}"
            sections.append({"key": new_key, "title": ch.get("title") or "",
                             "body": ch.get("body") or ch.get("to_text") or "",
                             "origin": "your own words"})
            added_keys[new_key] = _clause_key(ch)
    for k in removed_terms or []:
        token = "{{%s}}" % k
        for sec in sections:
            body = sec.get("body") or ""
            if token in body:
                sec["body"] = "\n".join(ln for ln in body.split("\n") if token not in ln)
    sections = [x for x in sections if (x.get("body") or "").strip()]
    ctx = cr._wording_context(s, c)
    sections, _topped = cw.missing_clauses(
        sections, values=ctx["values"], limits=new_terms, type_label=ctx["type_label"],
        dropped_sections=gone_sections, dropped_terms=gone_terms)
    sections = cw.renumber(sections)
    tokens = ctx["tokens"]
    by = mt.CREATED_BY
    for i, sec in enumerate(cr._composable(sections), start=1):
        words = cw.render(sec.get("body") or "", tokens).strip()
        header = f"{i}. {sec.get('title') or 'Section'}"
        cid = mirror.get(sec.get("key"))
        if cid:
            # Its words before, when they move — a term re-rendered into the
            # section is a change to what the row says even with no section
            # edit, and a setup made later rebuilds the old wording's checks
            # from this (date_setup_checks).
            was = s.execute(text(
                "SELECT title, text FROM clauses_extracted WHERE clause_id = :id "
                "AND contract_id = :c"), {"id": cid, "c": c.id}).first()
            if was is not None and (was[1] or "").strip() != words:
                done.setdefault("rerendered", []).append(
                    {"clause_id": int(cid), "from_title": was[0], "from_text": was[1],
                     "title": sec.get("title"), "to_text": words})
            s.execute(text(
                "UPDATE clauses_extracted SET title = :t, text = :x, section_header = :h, "
                "updated_at = now(), updated_by = :by WHERE clause_id = :id AND contract_id = :c "
                "AND (text IS DISTINCT FROM :x OR title IS DISTINCT FROM :t "
                "     OR section_header IS DISTINCT FROM :h)"),
                {"t": sec.get("title"), "x": words, "h": header, "by": by,
                 "id": cid, "c": c.id})
            sec["clause_id"] = int(cid)
        else:
            new_id = s.execute(text(
                "INSERT INTO clauses_extracted (contract_id, clause_type, title, text, "
                " section_header, rule_generation_status, generated_rule_count, "
                " extraction_confidence, created_by, updated_by) "
                "VALUES (:c, 'other', :t, :x, :h, 'pending', 0, 1.0, 'contract_wording', :by) "
                "RETURNING clause_id"),
                {"c": c.id, "t": sec.get("title"), "x": words, "h": header,
                 "by": by}).scalar()
            sec["clause_id"] = int(new_id)
            if sec.get("key") in added_keys:
                done["added"][added_keys[sec["key"]]] = int(new_id)
    for cid in done["deleted"]:
        s.execute(text(
            "UPDATE clauses_extracted SET rule_generation_status = 'not_rule_bearing', "
            "updated_at = now(), updated_by = :by WHERE clause_id = :id AND contract_id = :c"),
            {"by": by, "id": cid, "c": c.id})
    c.wording_sections = {**ws, "sections": sections, "dropped_sections": gone_sections,
                          "dropped_terms": sorted(set(gone_terms))}
    return done


def _open_clause_rules(s, c: Contract, a: ContractAmendment, plan: dict, done: dict,
                       day: dt.date, p: Principal) -> list[dict]:
    """The checks the re-worded and new clauses make, written the way
    Bordereau Setup writes a clause's checks (db_persister.persist_resolved_
    rules, inside this transaction) and then dated: in force from the change
    date, and owned by the change (source_amendment_id)."""
    from contract_upload_services import db_persister
    tids = plan.get("_tids") or {}
    groups: dict = {}
    for x in (plan.get("clause_rules") or {}).get("open") or []:
        key = x["clause_key"]
        cid = key if key > 0 else done["added"].get(key)
        if cid:
            groups.setdefault((x["set"], int(cid)), []).append(x["rule"])
    opened: list[dict] = []
    if not groups:
        return opened
    conn = s.connection()
    for (sid, cid), rules_ in groups.items():
        created = db_persister.persist_resolved_rules(
            contract_id=c.id, program_id=c.program_id, tenant_id=c.tenant_id,
            db_clause_id=cid, output_template_id=tids.get(sid),
            validation_rules=rules_, actor=f"user:{p.user_id}",
            created_by=mt.CREATED_BY, tag_template_id=sid, conn=conn,
            leave_review_queue=True)
        ids = [int(x["rule_id"]) for x in created]
        if not ids:
            continue
        s.execute(text(
            "UPDATE validation_rule SET effective_from = :d, source_amendment_id = :aid, "
            "updated_at = now() WHERE rule_id = ANY(:ids)"),
            {"d": day, "aid": a.id, "ids": ids})
        s.execute(text(
            "UPDATE clauses_extracted SET rule_generation_status = 'rules_generated', "
            "generated_rule_count = COALESCE(generated_rule_count, 0) + :n, "
            "updated_at = now() WHERE clause_id = :cid AND contract_id = :c"),
            {"n": len(ids), "cid": cid, "c": c.id})
        opened += [{"rule_id": i, "clause_id": cid, "set": sid} for i in ids]
    # A best-effort statement that failed inside the persister (its compiled-
    # SQL cache write) leaves the transaction aborted, and COMMIT would roll
    # the whole change back quietly. Asking once more fails it out loud.
    s.execute(text("SELECT 1"))
    return opened


def _carry_exclusions(s, c: Contract, pairs: list[tuple[int, int]]) -> list[int]:
    """A check a setup had excluded stays excluded in its new version."""
    import rule_scope
    touched = []
    if not pairs:
        return touched
    for pp in _setups(s, c):
        scope = rule_scope.pipeline_scope(s, pp.id)
        if not scope or not scope.get("excluded_rule_ids"):
            continue
        ex = [int(x) for x in scope["excluded_rule_ids"]]
        add = [new for old, new in pairs if old in ex and new not in ex]
        if add:
            rule_scope.write_pipeline_scope(s, pp.id, {**scope,
                                                       "excluded_rule_ids": ex + add})
            touched.append(pp.id)
    return touched


def _go_live(s, c: Contract, a: ContractAmendment, p: Principal) -> bool:
    """Apply the change, in the caller's transaction. Returns False — and
    leaves it SIGNED, with the reason on the record — when the plan can no
    longer be applied by itself (a check it replaces was edited, or Kavachio
    can't tell which check a term now lives in); the carrier then reviews and
    applies it."""
    c = _lock(s, c)
    stored = a.changes or {}
    req = stored.get("request") or {}
    body = MidTermIn(**{k: req.get(k) for k in MidTermIn.model_fields if k in req})
    plan = _plan(s, c, body, amendment=a,
                 stored_clause_rules=stored.get("clause_rules") or {})
    day = plan["_day"]
    hard = [b for b in plan["blockers"]
            if b["code"] in ("needs_choice", "effective_from", "state", "no_change",
                             "clauses", "no_checks")]
    then = {x["rule_id"]: x["fingerprint"] for x in
            ((stored.get("plan") or {}).get("close") or [])}
    now_fp = {x["rule_id"]: x["fingerprint"] for x in plan["rules"]["close"]}
    moved = sorted(rid for rid, fp in then.items() if now_fp.get(rid) != fp)
    if hard or moved or set(now_fp) != set(then):
        a.status = mt.SIGNED
        a.changes = {**stored, "held": {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "blockers": hard,
            "moved_rules": moved,
            "message": (hard[0]["message"] if hard else
                        "A check this change replaces was changed since it was agreed. "
                        "Review the checks and apply it.")}}
        return False

    start = c.inception_dt or (day - dt.timedelta(days=1))
    src = f"Mid-term change {a.number}, from {mt.fmt_date(day)}: " + "; ".join(
        _change_lines(a))
    closed = []
    for x in plan["rules"]["close"]:
        s.execute(text(
            "UPDATE validation_rule SET effective_to = :to, "
            # a rule written before mid-term changes holds its CREATION date in
            # effective_from (a column default); it has applied since the
            # contract began, and the table insists effective_to >= it.
            "effective_from = CASE WHEN source_amendment_id IS NULL "
            "  THEN LEAST(COALESCE(effective_from, :start), :start) ELSE effective_from END, "
            "updated_at = now() WHERE rule_id = :r"),
            {"to": day, "start": start, "r": x["rule_id"]})
        closed.append(x["rule_id"])
    opened, pairs = [], []
    for x in plan["rules"]["open"]:
        nid = _insert_rule(s, c, a, x, day, src)
        opened.append({"rule_id": nid, "replaces": x.get("replaces"), "key": x["key"]})
        if x.get("replaces"):
            pairs.append((int(x["replaces"]), nid))
    setups_touched = _carry_exclusions(s, c, pairs)

    # The wording: a written contract's terms and sections (its clause rows
    # re-worded in place — the checks name them); an uploaded one's clause rows.
    if not cr._is_uploaded(c):
        c.commercial_terms = plan["new_terms"] or None
        done = _apply_wording(s, c, a, plan["clause_changes"], plan["new_terms"],
                              [w["key"] for w in plan["changes"] if w["kind"] == "removed"])
    else:
        done = _apply_uploaded_clauses(s, c, plan["clause_changes"])
    opened_from_clauses = _open_clause_rules(s, c, a, plan, done, day, p)
    for st in plan["setups"]:
        pp = s.get(Pipeline, st["pipeline_id"])
        if pp is not None and st.get("date_column"):
            pp.governing_date_field = st["date_column"]
    a.status = mt.LIVE
    a.executed_date = cr._today()
    a.approved_by_id = a.approved_by_id or p.user_id
    a.changes = {**{k: v for k, v in stored.items() if k != "held"},
                 "applied": {"closed": closed, "opened": opened,
                             "opened_from_clauses": opened_from_clauses,
                             # checks the contract did not have before — what the
                             # setup may need mapping for (_setup_follow_up)
                             "new_checks": ([x["rule_id"] for x in opened if not x.get("replaces")]
                                            + [x["rule_id"] for x in opened_from_clauses]),
                             "clauses": {"amended": done["amended"],
                                         "added": sorted(done["added"].values()),
                                         "deleted": done["deleted"]},
                             # which clause row each added clause became, and
                             # the section rows a moved term re-worded — what a
                             # setup made later dates its checks by
                             "added_ids": {str(k): v for k, v in done["added"].items()},
                             "rerendered": done.get("rerendered") or [],
                             "clauses_deleted": done["deleted"],
                             # rule-bearing words the template has no column for
                             "clauses_without_column": [
                                 {k: x.get(k) for k in ("clause", "reason")}
                                 for x in (plan.get("clause_rules") or {}).get("no_column") or []],
                             "setups_exclusions_carried": setups_touched,
                             "recheck": plan["recheck"],
                             "at": dt.datetime.now(dt.timezone.utc).isoformat()}}
    _approval(s, c, a, p, "live")
    _activity(s, c, a, p, "live", {"rules_closed": closed, "rules_opened": opened,
                                   "rules_opened_from_clauses": opened_from_clauses,
                                   "files_to_recheck": len(plan["recheck"]),
                                   "changes": _change_lines(a)})
    cr._after_commit(s, _broker_email_thunks(s, c, a, "live"), "mid-term change live")
    return True


# =============================================================================
#  The record block
# =============================================================================

def _signed_sides(s, a: ContractAmendment) -> dict:
    doc_ids = [d.id for d in s.query(ContractDocument)
               .filter(ContractDocument.amendment_id == a.id).all()]
    if not doc_ids:
        return {}
    sigs = (s.query(ContractSignature)
            .filter(ContractSignature.document_id.in_(doc_ids))
            .order_by(ContractSignature.signed_at.asc()).all())
    return {sg.side: {"signer_name": sg.signer_name, "signer_title": sg.signer_title,
                      "method": sg.method,
                      "signed_at": sg.signed_at.isoformat() if sg.signed_at else None}
            for sg in sigs}


def _actions(s, c: Contract, a: ContractAmendment, p: Optional[Principal]) -> list[str]:
    if p is None or a.status not in mt.OPEN:
        return []
    if p.is_broker:
        if not cr._speaks_for_broker(p):
            return []
        return {"agree": ["agree", "request_changes"],
                "sign": ["sign"]}.get(mt.BROKER_STEP.get(a.status), [])
    out = ["withdraw"]
    if a.status == mt.PENDING and cr._speaks_for_carrier(p):
        out.append("accept")
    if a.status in (mt.WITH_BROKER, mt.CHANGES_REQUESTED):
        out.append("revise")
    if a.status == mt.AGREED and cr._speaks_for_carrier(p):
        out.append("sign")
    if a.status == mt.SIGNED:
        out.append("apply")
    return out


def _history(s, a: ContractAmendment) -> list[dict]:
    rows = (s.query(ContractApproval)
            .filter(ContractApproval.contract_id == a.contract_id,
                    ContractApproval.action.in_(list(mt.APPROVAL.values())))
            .order_by(ContractApproval.acted_at.asc()).all())
    words = {v: k for k, v in mt.APPROVAL.items()}
    out = []
    for r in rows:
        pc = r.proposed_changes if isinstance(r.proposed_changes, dict) else {}
        if pc.get("amendment_id") != a.id:
            continue
        out.append({"step": words.get(r.action, r.action), "note": r.note,
                    "at": cr._iso_utc(r.acted_at), "by": _user_name(s, r.acted_by_user_id)})
    return out


def _user_name(s, user_id: Optional[int]) -> Optional[str]:
    from db import AppUser
    u = s.get(AppUser, user_id) if user_id else None
    return (u.full_name or u.email) if u is not None else None


def _public(s, c: Contract, a: ContractAmendment, p: Optional[Principal],
            full: bool = True) -> dict:
    stored = a.changes or {}
    doc = _document(s, a)
    plan = stored.get("plan_public") or {}
    words = mt.BROKER_STATE_WORDS if (p is not None and p.is_broker) else mt.STATE_WORDS
    out = {
        "id": a.id, "number": int(a.number) if a.number else None,
        "status": a.status, "status_words": words.get(a.status, a.status),
        "road": stored.get("road"),
        "effective_from": str(a.effective_date) if a.effective_date else None,
        "boundary": mt.boundary_words(a.effective_date) if a.effective_date else None,
        "reason": a.description,
        "changes": stored.get("wording") or [],
        "clause_changes": [{k: v for k, v in ch.items() if k != "body"}
                           for ch in stored.get("clauses") or []],
        "created_at": cr._iso_utc(a.created_at),
        "executed_date": str(a.executed_date) if a.executed_date else None,
        "document": ({"id": doc.id, "filename": doc.filename} if doc else None),
        # What going live did: the checks closed and opened, and the files
        # already checked that may hold rows from the change date (D6).
        "applied": stored.get("applied"),
        # The checks it added, and whether each setup already feeds them.
        "setup_follow_up": (_setup_follow_up(s, c, a)
                            if a.status == mt.LIVE and p is not None and not p.is_broker
                            else None),
    }
    if not full:
        return out
    out.update({
        "rules": plan.get("rules"),
        "checks_unchanged": plan.get("checks_unchanged"),
        "clause_checks": plan.get("clause_checks"),
        "setups": [{k: v for k, v in st.items() if k != "columns"}
                   for st in (plan.get("setups") or [])],
        "signatures": _signed_sides(s, a),
        "actions": _actions(s, c, a, p),
        "history": _history(s, a),
        "held": stored.get("held"),
        "change_request": next((h["note"] for h in reversed(_history(s, a))
                                if h["step"] == "changes_requested"), None)
        if a.status == mt.CHANGES_REQUESTED else None,
        # What was sent, so a revision starts from it. The carrier's own.
        "request": (stored.get("request") if (p is not None and not p.is_broker)
                    else None),
    })
    return out


def _clause_history(s, c: Contract, live: list) -> dict[int, list[dict]]:
    """{clause id: what each live change did to it, oldest first} — from the
    clause changes each change kept (re-worded, added, deleted, and those
    re-worded only because a term in them moved) and, for a written contract,
    the section rows a moved term re-rendered. Plain values: read inside the
    caller's session, used after it."""
    out: dict[int, list[dict]] = {}
    for a in live:
        stored = a.changes or {}
        applied = stored.get("applied") or {}
        added_ids = applied.get("added_ids") or {}
        change = {"id": a.id, "date": a.effective_date,
                  "number": int(a.number) if a.number else None}
        added_rows = None
        for ch in stored.get("clauses") or []:
            cid = ch.get("clause_id")
            if ch.get("action") == mt.CLAUSE_ADDED and not cid:
                cid = added_ids.get(str(ch.get("index")))
                if cid is None:
                    # changes made before added_ids was kept: by title
                    if added_rows is None:
                        ids = (applied.get("clauses") or {}).get("added") or []
                        added_rows = {r[1]: r[0] for r in s.execute(text(
                            "SELECT clause_id, title FROM clauses_extracted "
                            "WHERE clause_id = ANY(:ids)"), {"ids": ids}).all()} if ids else {}
                    cid = added_rows.get(ch.get("title"))
            if not cid:
                continue
            out.setdefault(int(cid), []).append({
                "change": change, "action": ch.get("action"), "from_text": ch.get("from_text"),
                "from_title": ch.get("from_title") or ch.get("title"),
                "kind": ch.get("kind"), "section": ch.get("section")})
        seen = {int(ch["clause_id"]) for ch in stored.get("clauses") or [] if ch.get("clause_id")}
        for rr in applied.get("rerendered") or []:
            if int(rr["clause_id"]) in seen:
                continue
            out.setdefault(int(rr["clause_id"]), []).append({
                "change": change, "action": mt.CLAUSE_AMENDED, "from_text": rr.get("from_text"),
                "from_title": rr.get("from_title"), "kind": None, "section": None})
    return out


def date_setup_checks(*, contract_id: int, program_id: int, tenant_id,
                      template_fields: list, output_template_id: int,
                      actor: str) -> Optional[dict]:
    """A contract set up in Bordereau Setup AFTER a mid-term change went live.

    Its clause rows were re-worded in place when the change went live, so the
    checks a setup makes from them are the NEW wording's — left as they are, a
    row dated before the change would be checked on terms that did not apply
    to it yet (commission 24.5% on a 1 July row of a change from 15 July). So,
    for every clause a live change touched:

      · its undated checks (made from its current words) start on the date of
        the last change that touched it, owned by that change;
      · each earlier wording gets its own checks, made from those words by the
        same step (mid_term_ai.rules_for_clauses — Pipeline 2, cached), for the
        days it applied: the original wording up to the first change, then
        each change's words up to the next one.

    Exactly what the contract holds had it been set up before the change — one
    row per check version, nothing duplicated. A deleted clause gets its old
    wording's checks up to the day it was deleted, none after.

    Safe to run again: a clause that already has dated checks (set up before
    the change, or rebuilt here already) is left alone, so a contract set up
    the normal way is untouched and an interrupted rebuild finishes. Returns
    what it did — None when there is no live change — and reports, never
    raises, an AI failure: the current checks are dated either way."""
    from db import SessionLocal
    with SessionLocal() as s:
        c = s.get(Contract, contract_id)
        if c is None:
            return None
        live = sorted([a for a in _amendments(s, c) if a.status == mt.LIVE and a.effective_date],
                      key=lambda a: (a.effective_date, a.id))
        if not live:
            return None
        numbers = [int(a.number) for a in live if a.number]
        history = _clause_history(s, c, live)
        if not history:
            return None
        rows = s.execute(text(
            "SELECT rule_id, source_clause_id, effective_to, source_amendment_id "
            "FROM validation_rule WHERE contract_id = :c AND source_clause_id = ANY(:ids)"),
            {"c": contract_id, "ids": list(history)}).all()
        undated: dict[int, list[int]] = {}
        dated = set()
        for rid, cid, eto, aid in rows:
            if eto is None and aid is None:
                undated.setdefault(int(cid), []).append(int(rid))
            else:
                dated.add(int(cid))
        start = c.inception_dt
        dated_current, windows = 0, []
        for cid, steps in history.items():
            if cid in dated:
                continue
            last = steps[-1]
            if last["action"] != mt.CLAUSE_REMOVED and undated.get(cid):
                s.execute(text(
                    "UPDATE validation_rule SET effective_from = :d, source_amendment_id = :aid, "
                    "updated_at = now() WHERE rule_id = ANY(:ids)"),
                    {"d": last["change"]["date"], "aid": last["change"]["id"],
                     "ids": undated[cid]})
                dated_current += len(undated[cid])
            prev = None
            for n, st in enumerate(steps):
                if st["action"] != mt.CLAUSE_ADDED and (st["from_text"] or "").strip():
                    windows.append({"round": n, "clause_id": cid, "text": st["from_text"],
                                    "title": st["from_title"], "kind": st["kind"],
                                    "section": st["section"], "since": prev,
                                    "until": st["change"]["date"]})
                prev = st["change"]
        s.commit()

    made, unbound_all, error = [], {}, None
    from contract_upload_services import db_persister
    for rnd in sorted({w["round"] for w in windows}):
        batch = [w for w in windows if w["round"] == rnd]
        by_clause, unbound, err = mta.rules_for_clauses(
            [{"clause_id": w["clause_id"], "clause_type": w["kind"], "title": w["title"],
              "text": w["text"], "section_header": w["section"]} for w in batch],
            template_fields, contract_id=contract_id, tenant_id=tenant_id,
            program_id=program_id)
        if err:
            error = err
            break
        unbound_all.update({str(k): v for k, v in unbound.items()})
        for w in batch:
            rules_ = by_clause.get(w["clause_id"]) or []
            if not rules_:
                continue
            created_old = db_persister.persist_resolved_rules(
                contract_id=contract_id, program_id=program_id, tenant_id=tenant_id,
                db_clause_id=w["clause_id"], output_template_id=output_template_id,
                validation_rules=rules_, actor=actor, created_by=mt.CREATED_BY,
                leave_review_queue=True)
            old_ids = [int(x["rule_id"]) for x in created_old if x.get("rule_id")]
            if not old_ids:
                continue
            since = w["since"]
            with SessionLocal() as s2:
                s2.execute(text(
                    "UPDATE validation_rule SET effective_to = :to, "
                    + ("effective_from = :fr, source_amendment_id = :aid, " if since else
                       # the original wording: in force since the contract began
                       "effective_from = LEAST(COALESCE(effective_from, :st), :st), ")
                    + "updated_at = now() WHERE rule_id = ANY(:ids)"),
                    {"to": w["until"], "ids": old_ids, "st": start or w["until"],
                     **({"fr": since["date"], "aid": since["id"]} if since else {})})
                s2.commit()
            made.append({"clause_id": w["clause_id"], "rules": old_ids,
                         "until": str(w["until"]),
                         "since": str(since["date"]) if since else None})
    if not (dated_current or made or error):
        return None
    return {"changes": numbers, "clauses": len(history),
            "current_checks_dated": dated_current,
            "earlier_checks": sum(len(m["rules"]) for m in made), "earlier": made,
            "no_column": unbound_all, "error": error}


def refresh_open_change(s, c: Contract, p: Optional[Principal]) -> Optional[dict]:
    """The contract has just been set up (contract_routes.use_setup) while a
    change was in progress. What was sent and agreed — its terms, wording and
    date — stays exactly as it is; which checks it closes and opens is worked
    out again against the checks the contract has now. Left alone, go-live
    would meet checks the stored plan never named and hold the change. The
    caller commits."""
    a = _open_amendment(s, c)
    if a is None:
        return None
    stored = a.changes or {}
    req = stored.get("request") or {}
    body = MidTermIn(**{k: req.get(k) for k in MidTermIn.model_fields if k in req})
    plan = _plan(s, c, body, amendment=a, work_out=True)
    fresh = _store(plan, body)
    a.changes = {**stored, "clause_rules": fresh["clause_rules"], "plan": fresh["plan"],
                 "plan_public": {**(stored.get("plan_public") or {}),
                                 **fresh["plan_public"]}}
    cp = plan.get("clause_rules") or {}
    return {"id": a.id, "number": int(a.number) if a.number else None,
            "status": a.status,
            "closing": len(plan["rules"]["close"]),
            "opening": len(plan["rules"]["open"]) + len(cp.get("open") or []),
            "blockers": [b["code"] for b in plan["blockers"]]}


def record_block(s, c: Contract, p: Optional[Principal]) -> Optional[dict]:
    """What the contract page needs about mid-term changes."""
    all_ = _amendments(s, c)
    open_ = next((a for a in all_ if a.status in mt.OPEN), None)
    lifecycle = cr._effective_lifecycle(c)
    why = None
    if lifecycle not in ("active", "expired"):
        why = (f"A mid-term change is made to a running contract; this one is "
               f"{lifecycle}.")
    elif open_ is not None:
        why = f"Mid-term change {open_.number} is in progress."
    is_carrier = p is not None and not p.is_broker
    if why is None and is_carrier:
        import contract_checks as _cc
        _serving = _cc.serving_setup(s, c)
        if _serving is None or not _cc.check_count(s, _serving, c.id):
            why = NO_CHECKS_WORDS
    return {
        "can_start": bool(is_carrier and why is None),
        "why_not": why if is_carrier else None,
        "open": _public(s, c, open_, p) if open_ is not None else None,
        "history": [_public(s, c, a, p, full=False) for a in all_
                    if a.status in (mt.LIVE, mt.WITHDRAWN)],
        "latest_live_from": (str(_latest_live_from(s, c))
                             if _latest_live_from(s, c) else None),
    }


def open_changes(s, contract_ids) -> dict[int, dict]:
    """The change in progress on each of these contracts, said from the
    broker's side: {contract_id: {id, number, effective_from, status,
    status_words, step}}, where `step` is set only when the move is the
    broker's (mt.BROKER_STEP).

    For the broker's dashboard and contract list. Both read the contract's own
    lifecycle, and a running contract with a change sent over is simply
    "Active" there — the email was the only sign the broker had anything to do."""
    ids = [i for i in contract_ids if i is not None]
    if not ids:
        return {}
    rows = (s.query(ContractAmendment)
            .filter(ContractAmendment.contract_id.in_(ids),
                    ContractAmendment.kind == mt.KIND,
                    ContractAmendment.status.in_(list(mt.OPEN)))
            .order_by(ContractAmendment.id.desc()).all())
    out: dict[int, dict] = {}
    for a in rows:
        out.setdefault(a.contract_id, {
            "id": a.id, "number": int(a.number) if a.number else None,
            "effective_from": str(a.effective_date) if a.effective_date else None,
            "status": a.status,
            "status_words": mt.BROKER_STATE_WORDS.get(a.status, a.status),
            "step": mt.BROKER_STEP.get(a.status),
        })
    return out


# =============================================================================
#  Routes
# =============================================================================

@router.get("/contracts/{contract_id}/mid-term")
def mid_term_get(contract_id: int, p: Principal = Depends(current_principal)):
    """Where a change can start from: the current terms (typed, or read off an
    uploaded contract's checks), the setups that run it, and the change in
    progress."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        rules = _rules_of(s, c)
        fields_by_set, _tids = _fields_by_set(s, c, rules)
        block = record_block(s, c, p) or {}
        plan = _plan(s, c, MidTermIn(), amendment=_open_amendment(s, c), read="ask") \
            if not p.is_broker else None
        clauses = _current_clauses(s, c, rules) if not p.is_broker else None
        tokens = (cr._wording_context(s, c)["tokens"]
                  if (not p.is_broker and not cr._is_uploaded(c)) else None)
        return {
            **block,
            "contract": {"id": c.id, "name": c.name or c.filename,
                         "inception_dt": str(c.inception_dt) if c.inception_dt else None,
                         "expiry_dt": str(c.expiry_dt) if c.expiry_dt else None,
                         "is_uploaded": cr._is_uploaded(c),
                         "programme": getattr(s.get(Program, c.program_id), "name", None)
                         if c.program_id else None,
                         "counterparty": getattr(s.get(Party, c.broker_party_id),
                                                 "legal_name", None)
                         if c.broker_party_id else None},
            "current_terms": (plan or {}).get("current_terms"),
            "terms_error": (plan or {}).get("terms_error"),
            "setups": (plan or {}).get("setups"),
            # What the contract says today — the clauses a change can re-word,
            # delete, or add to. A written contract's are its sections, with
            # their terms as tokens (`tokens` is what each stands for now).
            "clauses": clauses,
            "tokens": tokens,
        }


@router.post("/contracts/{contract_id}/mid-term/preview")
def mid_term_preview(contract_id: int, body: MidTermIn,
                     p: Principal = Depends(current_principal)):
    """What this change would say and do. Writes nothing."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        cr._require_carrier(p)
        return _public_plan(_plan(s, c, body, amendment=_open_amendment(s, c)
                                  if _is_revising(s, c) else None,
                                  work_out=body.work_out_checks))


def _is_revising(s, c: Contract) -> bool:
    a = _open_amendment(s, c)
    return a is not None and a.status in (mt.WITH_BROKER, mt.CHANGES_REQUESTED)


def _store(plan: dict, body: MidTermIn) -> dict:
    """What a change keeps of its plan: the request (to work the plan out
    again at go-live), the wording of the change, the checks its re-worded and
    new clauses make (in full — go-live writes these, never asks again), and
    the plan as shown."""
    pub = _public_plan(plan)
    return {
        "road": plan["road"],
        "request": {"effective_from": body.effective_from, "reason": body.reason,
                    "agreed_limits": plan["new_terms"], "choices": body.choices or {},
                    "date_columns": {str(st["pipeline_id"]): st["date_column"]
                                     for st in plan["setups"] if st.get("date_column")},
                    "sections": plan["sections"],
                    "clauses": body.clauses or [],
                    "document_id": (plan.get("document") or {}).get("id"),
                    "signed": bool(plan.get("signed"))},
        "old_terms": plan["current_terms"],
        "wording": plan["changes"],
        "clauses": [{**_public_clause(ch), **({"body": ch["body"]} if "body" in ch else {})}
                    for ch in plan["clause_changes"]],
        "clause_rules": (plan.get("clause_rules") or {}).get("generated") or {},
        "plan": {"close": plan["rules"]["close"],
                 "open": [{k: v for k, v in x.items() if k != "rule_spec"}
                          for x in plan["rules"]["open"]]},
        "plan_public": {k: pub[k] for k in ("rules", "clause_checks", "checks_unchanged",
                                            "setups", "recheck", "boundary")},
    }


@router.post("/contracts/{contract_id}/mid-term")
def mid_term_send(contract_id: int, body: MidTermIn,
                  p: Principal = Depends(current_principal)):
    """Make the change and send it to the broker for agreement."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        cr._require_carrier(p)
        c = _lock(s, c)
        plan = _plan(s, c, body, work_out=True)
        _raise_blocked(plan)
        signed_paper = plan["road"] == mt.ROAD_UPLOADED_SIGNED
        a = ContractAmendment(
            tenant_id=c.tenant_id, ops_contract_id=c.id, contract_id=c.id,
            number=str(plan["number"]), kind=mt.KIND, effective_date=plan["_day"],
            description=(body.reason or "").strip(),
            status=mt.PENDING if signed_paper else mt.WITH_BROKER)
        s.add(a)
        s.flush()
        a.changes = _store(plan, body)
        doc = (_attach_document(s, c, a, s.get(ContractDocument, plan["document"]["id"]))
               if plan["document"] else _compose_document(s, c, a, plan, p))
        a.changes = {**a.changes, "document_id": doc.id}
        if signed_paper:
            # Signed on paper by both sides: nothing to negotiate and nobody
            # left to sign. The carrier admin accepts it (a carrier user's
            # upload waits for them, as an uploaded contract does) and it goes
            # live; the broker is emailed then (D7).
            _approval(s, c, a, p, "uploaded", note=a.description)
            _activity(s, c, a, p, "uploaded", {"changes": _change_lines(a),
                                               "reason": a.description,
                                               "document": doc.filename})
            _accept_or_wait(s, c, a, p)
        else:
            _approval(s, c, a, p, "sent", note=a.description)
            _activity(s, c, a, p, "sent", {"changes": _change_lines(a),
                                           "reason": a.description})
            cr._after_commit(s, _broker_email_thunks(s, c, a, "sent"), "mid-term change")
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)


def _accept_or_wait(s, c: Contract, a: ContractAmendment, p: Principal) -> None:
    """A signed change document: the carrier admin's word puts it live; a
    carrier user's upload waits for it (carrier_scope.needs_carrier_approval)."""
    import carrier_scope
    if carrier_scope.needs_carrier_approval(s, p) or not cr._speaks_for_carrier(p):
        a.status = mt.PENDING
        _notify_carrier(s, c, a, p, f"Mid-term change {a.number} needs your acceptance",
                        "A signed change document was uploaded. It goes live when you "
                        "accept it.", "Waiting on you to accept")
        return
    a.approved_by_id = p.user_id
    _approval(s, c, a, p, "accepted")
    _activity(s, c, a, p, "accepted")
    s.flush()
    _go_live(s, c, a, p)


@router.post("/contracts/{contract_id}/mid-term/{amendment_id}/accept")
def mid_term_accept(contract_id: int, amendment_id: int,
                    p: Principal = Depends(current_principal)):
    """The carrier admin accepts a signed change document uploaded by a
    carrier user. It goes live."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        cr._require_carrier(p)
        cr._assert_speaks_for_carrier(p, "accept a mid-term change")
        c = _lock(s, c)
        a = _amendment(s, c, amendment_id)
        if a.status != mt.PENDING:
            raise HTTPException(409, f"Mid-term change {a.number} is not waiting to be "
                                     f"accepted.")
        a.approved_by_id = p.user_id
        _approval(s, c, a, p, "accepted")
        _activity(s, c, a, p, "accepted")
        s.flush()
        _go_live(s, c, a, p)
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)


@router.post("/contracts/{contract_id}/mid-term/document")
def mid_term_document(contract_id: int, file: UploadFile = File(...),
                      p: Principal = Depends(current_principal)):
    """A signed change document (an endorsement PDF): stored, and read for
    what it changes — the terms it moves and the clauses it re-words, adds or
    deletes, matched against this contract's clauses (mid_term_ai.
    read_change_document). Nothing about the contract changes here: the
    carrier reviews what was read on the change screen, and the document is
    attached to the change when it is sent."""
    from contract_upload_services.constants import MAX_CONTRACT_UPLOAD_BYTES
    import storage
    data = file.file.read()
    name = os.path.basename(file.filename or "change.pdf")
    if not data:
        raise HTTPException(400, {"message": "The file is empty.", "errors": {"file": "empty"}})
    if len(data) > MAX_CONTRACT_UPLOAD_BYTES:
        raise HTTPException(400, {"message": "The file is too large for a change document.",
                                  "errors": {"file": "too_large"}})
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, {"message": "Upload the change document as a PDF.",
                                  "errors": {"file": "not_pdf"}})
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        cr._require_carrier(p)
        lifecycle = cr._effective_lifecycle(c)
        if lifecycle not in ("active", "expired"):
            raise HTTPException(409, f"This contract is {lifecycle}. A mid-term change is "
                                     f"made to a running contract.")
        try:
            doc_text = cr._parse_to_text(data, name)
        except Exception as e:  # noqa: BLE001 — a PDF Kavachio can't open
            log.warning("mid-term document %s not readable: %s", name, e)
            raise HTTPException(400, {"message": "Kavachio couldn't open this PDF. Is it "
                                                 "password-protected or damaged?",
                                      "errors": {"file": "unreadable"}})
        current = _current_clauses(s, c)
        # A written contract's sections have no clause id of their own to quote
        # back; they are numbered for the reading and mapped back by ref.
        asked = [{**x, "clause_id": x["clause_id"] if cr._is_uploaded(c) else i}
                 for i, x in enumerate(current, start=1)
                 if x.get("clause_id") or not cr._is_uploaded(c)]
        read, err = mta.read_change_document(doc_text, asked, c.tenant_id)
        blob_ref, blob = storage.store_or_keep("contract-documents", c.tenant_id, name, data,
                                               "application/pdf")
        doc = ContractDocument(
            tenant_id=c.tenant_id, contract_id=c.id, kind="endorsement", filename=name,
            version="mid-term-upload", blob_ref=blob_ref, blob=blob,
            fingerprint=hashlib.sha256(data).hexdigest(), is_active=False,
            is_executed_copy=True, uploaded_by_user_id=p.user_id,
            extracted={"mid_term_upload": True, "read": read, "read_error": err})
        s.add(doc)
        s.commit()
        return {"document": {"id": doc.id, "filename": doc.filename},
                "read": read, "error": err}


@router.post("/contracts/{contract_id}/mid-term/{amendment_id}/revise")
def mid_term_revise(contract_id: int, amendment_id: int, body: MidTermIn,
                    p: Principal = Depends(current_principal)):
    """Change what was sent — after the broker asked for changes, or before
    they answered — and send it again. A new document replaces the old one."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        cr._require_carrier(p)
        c = _lock(s, c)
        a = _amendment(s, c, amendment_id)
        if a.status not in (mt.WITH_BROKER, mt.CHANGES_REQUESTED):
            raise HTTPException(409, f"Mid-term change {a.number} is "
                                     f"{mt.STATE_WORDS.get(a.status, a.status).lower()} "
                                     f"and can't be revised now.")
        plan = _plan(s, c, body, amendment=a, work_out=True)
        _raise_blocked(plan)
        if plan["road"] == mt.ROAD_UPLOADED_SIGNED:
            raise HTTPException(409, "A change sent to the broker is revised and sent "
                                     "again; to use a document signed by both sides, "
                                     "withdraw this change and start a new one.")
        a.effective_date = plan["_day"]
        a.description = (body.reason or "").strip()
        a.status = mt.WITH_BROKER
        a.changes = _store(plan, body)
        doc = (_attach_document(s, c, a, s.get(ContractDocument, plan["document"]["id"]))
               if plan["document"] else _compose_document(s, c, a, plan, p))
        a.changes = {**a.changes, "document_id": doc.id}
        _approval(s, c, a, p, "sent", note=a.description, extra={"revised": True})
        _activity(s, c, a, p, "sent", {"changes": _change_lines(a), "revised": True,
                                       "reason": a.description})
        cr._after_commit(s, _broker_email_thunks(s, c, a, "sent"), "mid-term change")
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)


@router.post("/contracts/{contract_id}/mid-term/{amendment_id}/agree")
def mid_term_agree(contract_id: int, amendment_id: int,
                   p: Principal = Depends(current_principal)):
    """The broker agrees the change. It then needs both signatures."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        if not p.is_broker:
            raise HTTPException(403, "only the broker agrees a mid-term change")
        cr._assert_speaks_for_broker(p, "agree a mid-term change")
        c = _lock(s, c)
        a = _amendment(s, c, amendment_id)
        if a.status != mt.WITH_BROKER:
            raise HTTPException(409, f"Mid-term change {a.number} is not waiting for "
                                     f"your agreement.")
        a.status = mt.AGREED
        _approval(s, c, a, p, "agreed")
        _activity(s, c, a, p, "agreed")
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        _notify_carrier(s, c, a, p,
                        f"{getattr(party, 'legal_name', None) or 'The broker'} agreed "
                        f"mid-term change {a.number} — it needs your signature",
                        "The broker has agreed the change. It goes live once both sides "
                        "have signed it, and the carrier signs first.",
                        "Waiting on you to sign")
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)


@router.post("/contracts/{contract_id}/mid-term/{amendment_id}/request-changes")
def mid_term_request_changes(contract_id: int, amendment_id: int, body: MidTermNote,
                             p: Principal = Depends(current_principal)):
    """The broker sends the change back with what they want different."""
    note = (body.note or "").strip()
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        if not p.is_broker:
            raise HTTPException(403, "only the broker can ask for changes here")
        cr._assert_speaks_for_broker(p, "ask for changes to a mid-term change")
        if not note:
            raise HTTPException(400, {"message": "Say what you want changed.",
                                      "errors": {"note": "required"}})
        c = _lock(s, c)
        a = _amendment(s, c, amendment_id)
        if a.status != mt.WITH_BROKER:
            raise HTTPException(409, f"Mid-term change {a.number} is not waiting for "
                                     f"your answer.")
        a.status = mt.CHANGES_REQUESTED
        _approval(s, c, a, p, "changes_requested", note=note)
        _activity(s, c, a, p, "changes_requested", {"note": note})
        party = s.get(Party, c.broker_party_id) if c.broker_party_id else None
        _notify_carrier(s, c, a, p,
                        f"{getattr(party, 'legal_name', None) or 'The broker'} asked for "
                        f"changes to mid-term change {a.number}",
                        f"What they asked: {note}",
                        "Waiting on you to revise or withdraw it")
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)


@router.post("/contracts/{contract_id}/mid-term/{amendment_id}/withdraw")
def mid_term_withdraw(contract_id: int, amendment_id: int, body: MidTermNote,
                      p: Principal = Depends(current_principal)):
    """Drop a change that is not live yet. Nothing was applied, so nothing is
    undone; its document is kept, retired."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        cr._require_carrier(p)
        c = _lock(s, c)
        a = _amendment(s, c, amendment_id)
        if a.status not in mt.OPEN:
            raise HTTPException(409, f"Mid-term change {a.number} is "
                                     f"{mt.STATE_WORDS.get(a.status, a.status).lower()}.")
        a.status = mt.WITHDRAWN
        for d in (s.query(ContractDocument)
                  .filter(ContractDocument.amendment_id == a.id).all()):
            d.is_active = False
        _approval(s, c, a, p, "withdrawn", note=(body.note or "").strip() or None)
        _activity(s, c, a, p, "withdrawn", {"note": (body.note or "").strip() or None})
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)


@router.post("/contracts/{contract_id}/mid-term/{amendment_id}/apply")
def mid_term_apply(contract_id: int, amendment_id: int, body: MidTermIn,
                   p: Principal = Depends(current_principal)):
    """A signed change the go-live had to hold: the carrier settles what held
    it (which check a term now lives in) and applies it."""
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        cr._require_carrier(p)
        c = _lock(s, c)
        a = _amendment(s, c, amendment_id)
        if a.status != mt.SIGNED:
            raise HTTPException(409, f"Mid-term change {a.number} is not waiting to "
                                     f"be applied.")
        stored = a.changes or {}
        req = dict(stored.get("request") or {})
        if body.choices:
            req["choices"] = {**(req.get("choices") or {}), **body.choices}
        # The clause checks are worked out again too: what held the change may
        # be checks the contract did not have when it was sent (set up since).
        plan = _plan(s, c, MidTermIn(**{k: req.get(k) for k in MidTermIn.model_fields
                                         if k in req}), amendment=a, work_out=True)
        # Agreed terms don't move here — only which checks carry them.
        a.changes = {**stored, "request": req,
                     "clause_rules": (plan.get("clause_rules") or {}).get("generated") or {},
                     "plan": {"close": plan["rules"]["close"],
                              "open": [{k: v for k, v in x.items() if k != "rule_spec"}
                                       for x in plan["rules"]["open"]]},
                     "plan_public": {**(stored.get("plan_public") or {}),
                                     "rules": _public_plan(plan)["rules"]}}
        if not _go_live(s, c, a, p):
            s.commit()
            held = (a.changes or {}).get("held") or {}
            raise HTTPException(409, {"message": held.get("message") or "Not applied.",
                                      "errors": {}, "held": held})
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)


class MidTermSign(BaseModel):
    """A typed signature on the change document. The name defaults to the
    signing person's own; the title is optional."""
    signer_name: Optional[str] = None
    signer_title: Optional[str] = None


@router.post("/contracts/{contract_id}/mid-term/{amendment_id}/sign")
def mid_term_sign(contract_id: int, amendment_id: int, body: MidTermSign = MidTermSign(),
                  p: Principal = Depends(current_principal)):
    """Sign the change document — the carrier first, then the broker, the same
    order as a contract. The broker's signature puts the change live."""
    from db import AppUser
    with SessionLocal() as s:
        c = _gate(s, p, contract_id)
        c = _lock(s, c)
        a = _amendment(s, c, amendment_id)
        doc = _document(s, a)
        if doc is None:
            raise HTTPException(409, f"Mid-term change {a.number} has no document to sign.")
        if p.is_broker:
            cr._assert_speaks_for_broker(p, "sign a mid-term change")
            if a.status == mt.AGREED:
                raise HTTPException(409, "The carrier signs first — you can sign once "
                                         "they have.")
            if a.status != mt.CARRIER_SIGNED:
                raise HTTPException(409, f"Mid-term change {a.number} is not waiting for "
                                         f"your signature.")
            side = "counterparty"
        else:
            cr._assert_speaks_for_carrier(p, "sign a mid-term change")
            if a.status != mt.AGREED:
                raise HTTPException(409, f"Mid-term change {a.number} is not waiting for "
                                         f"the carrier's signature"
                                         + (" — the broker has not agreed it yet."
                                            if a.status == mt.WITH_BROKER else "."))
            side = "carrier"
        u = s.get(AppUser, p.user_id)
        name = (body.signer_name or "").strip() or (u.full_name if u else None) \
            or (u.email if u else None)
        if not name:
            raise HTTPException(400, {"message": "Type your name to sign.",
                                      "errors": {"signer_name": "required"}})
        s.add(ContractSignature(
            tenant_id=c.tenant_id, contract_id=c.id, side=side, signer_name=name,
            signer_title=(body.signer_title or "").strip() or None,
            signer_email=u.email if u else None, method="typed", by_user_id=p.user_id,
            # Aware, as contract signing writes it: a naive utcnow() lands in
            # this timestamptz column as India time, 5h30 early.
            signed_at=dt.datetime.now(dt.timezone.utc), document_id=doc.id,
            note=f"Mid-term change {a.number}"))
        _approval(s, c, a, p, "signed", note=f"Signed for the "
                  f"{'carrier' if side == 'carrier' else 'broker'} by {name}",
                  extra={"side": side})
        if side == "carrier":
            a.status = mt.CARRIER_SIGNED
            a.approved_by_id = p.user_id
            _activity(s, c, a, p, "signed", {"side": side, "signer": name})
        else:
            a.status = mt.SIGNED
            _activity(s, c, a, p, "signed", {"side": side, "signer": name})
            s.flush()
            _go_live(s, c, a, p)
        s.commit()
        s.refresh(c)
        return cr._record(s, c, p=p)
