"""
Mid-term change — the rules, with no database (Kavachio_Mid_Term_Change_Plan.md).

A mid-term change is a request attached to a RUNNING contract: some of its
terms change from a date. The contract stays the same row. The change goes to
the broker, is agreed and signed (or, for an uploaded contract with a signed
change document, accepted by the carrier), and only then goes live. Going live
never overwrites a check: each check the change touches is CLOSED on its
effective date and a new version OPENED from it, so every bordereau row is
judged by the version in force on its transaction effective date
(rule_windows.py does that at run time).

This module answers, from plain values:
  * the words for a change's states and history;
  * whether an effective date is acceptable;
  * the RULE PLAN — which existing checks a change of terms touches, and what
    each new version is — for a contract written in Kavachio (its term checks
    carry the term's key) and for an uploaded one (its checks were read from
    the PDF, so they are found by column and by the value the term had).

  * which CLAUSES the change re-words, adds or deletes — the carrier edits
    the clauses as they read today, and what actually moved is worked out
    here, the way a term's change is.

mid_term_routes reads the facts from the database, asks these functions, and
writes the result. The tests ask them directly.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from typing import Any, Callable, Optional

import rule_windows as rw

KIND = "mid_term"

# ── states ──────────────────────────────────────────────────────────────────
WITH_BROKER = "with_broker"
CHANGES_REQUESTED = "changes_requested"
AGREED = "agreed"
CARRIER_SIGNED = "carrier_signed"
SIGNED = "signed"          # both sides signed, but it could not be applied yet
PENDING = "pending"        # an uploaded change waiting for the carrier admin
LIVE = "live"
WITHDRAWN = "withdrawn"
OPEN = (WITH_BROKER, CHANGES_REQUESTED, AGREED, CARRIER_SIGNED, SIGNED, PENDING)

STATE_WORDS = {
    WITH_BROKER: "With broker", CHANGES_REQUESTED: "Changes requested",
    AGREED: "Agreed — waiting for signatures", CARRIER_SIGNED: "Signed by carrier",
    SIGNED: "Signed — not yet applied", PENDING: "Waiting for the carrier to accept",
    LIVE: "Live", WITHDRAWN: "Withdrawn",
}

# What an open change waits for the BROKER to do: read it and agree it (or ask
# for changes), or sign it once the carrier has. Every other open state is the
# carrier's move. _actions offers the broker exactly these, and the broker's
# dashboard and contract list count them — one rule, so the queue and the
# buttons can never disagree.
BROKER_STEP = {WITH_BROKER: "agree", CARRIER_SIGNED: "sign"}

# The same states said from the broker's side of the table. "With broker" is
# the carrier's word for it; read by the broker it named somebody else's queue.
BROKER_STATE_WORDS = {
    **STATE_WORDS,
    WITH_BROKER: "For your review",
    CHANGES_REQUESTED: "You asked for changes — the carrier is revising",
    AGREED: "You agreed — the carrier signs first",
    CARRIER_SIGNED: "Signed by the carrier — yours to sign",
    SIGNED: "Signed by both sides — waiting on the carrier",
}

# How the change was made — decides who must agree (plan §4).
ROAD_AUTHORED = "authored"            # contract written in Kavachio
ROAD_UPLOADED_TYPED = "uploaded_typed"    # uploaded contract, change typed here
ROAD_UPLOADED_SIGNED = "uploaded_signed"  # uploaded contract, signed PDF uploaded

# contract_approval.approval_action — the History stepper's words.
APPROVAL = {
    "sent": "mid_term_sent", "uploaded": "mid_term_uploaded",
    "changes_requested": "mid_term_changes_requested",
    "agreed": "mid_term_agreed", "signed": "mid_term_signed",
    "accepted": "mid_term_accepted", "live": "mid_term_live",
    "withdrawn": "mid_term_withdrawn",
}
# activity_events.action — the audit log and the bell.
ACTIVITY = {k: f"contract_{v}" for k, v in APPROVAL.items()}

CREATED_BY = "mid_term_change"


def fmt_date(d: Optional[dt.date]) -> str:
    return f"{d.day} {d:%b %Y}" if d else "—"


# ── the effective date ──────────────────────────────────────────────────────

def effective_date_errors(effective: Optional[dt.date], inception: Optional[dt.date],
                          expiry: Optional[dt.date],
                          latest_live: Optional[dt.date] = None) -> dict:
    """{field: why} when the date can't carry a mid-term change; {} when it can.

    After the start (a change from the first day is the contract itself, not a
    change to it — edit or correct it instead), no later than the end, and after
    the last live change: back-dating behind one would leave the windows
    overlapping, and an earlier change is undone by a new one, not hidden under
    it."""
    if effective is None:
        return {"effective_from": "Say from which date the change applies."}
    if inception and effective <= inception:
        return {"effective_from": (
            f"A mid-term change starts after the contract does ({fmt_date(inception)}). "
            "A change from the first day is a correction — use Change period, or "
            "edit a draft.")}
    if expiry and effective > expiry:
        return {"effective_from": (
            f"The contract ends on {fmt_date(expiry)}, so a change can't start "
            "after that. To run it for longer, change its end date as part of the "
            "same mid-term change.")}
    if latest_live and effective <= latest_live:
        return {"effective_from": (
            f"An earlier mid-term change already applies from {fmt_date(latest_live)}. "
            "A new one has to start after it.")}
    return {}


def boundary_words(effective: dt.date) -> dict:
    """The two sides of the change date, said the way the preview shows them so
    nobody has to work out which side the day itself falls on."""
    return {"old_until": fmt_date(effective - dt.timedelta(days=1)),
            "new_from": fmt_date(effective),
            "sentence": (f"Rows with a transaction effective date up to and including "
                         f"{fmt_date(effective - dt.timedelta(days=1))} are checked on the "
                         f"current terms; rows dated {fmt_date(effective)} or later on "
                         f"the new terms.")}


# ── values and the rule a term became ───────────────────────────────────────

def _num(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _same(a, b) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    return str(a).strip().lower() == str(b).strip().lower()


def _show(v) -> str:
    """A value as the carrier typed it: 25.5, not 25.500000."""
    n = _num(v)
    if n is None:
        return str(v)
    return str(int(n)) if n == int(n) else f"{n:g}"


# For each comparison a term makes, which rule template it became and which of
# that template's parameters hold the value (contract_rules._ir_for).
def _bounds(op: str, kind: str) -> list[tuple[str, tuple[str, ...]]]:
    if op == "eq":
        if kind in ("percent", "money", "int"):
            return [("range_check", ("min", "max"))]
        return [("value_in_set", ("allowed",))]
    return {
        "lte": [("max_limit", ("max",)), ("range_check", ("max",))],
        "lte_unless_referred": [("max_limit", ("max",))],
        "gte": [("min_limit", ("min",)), ("range_check", ("min",))],
        "lte_aggregate": [("aggregate_cap", ("max",))],
        "in": [("value_in_set", ("allowed",))],
        "not_in": [("value_not_in_set", ("excluded",))],
    }.get(op, [])


def _shape_fits(template: str, params: tuple, p: dict) -> bool:
    """A range_check stands for ONE comparison. With min = max it is an agreed
    value (eq); with only a max it is a cap (lte); with only a min, a floor
    (gte). Read as a cap when it also has a floor, "commission is 23.5" would
    be taken for "commission may not exceed 23.5" as well — one check, two
    terms, and a change to either would move it."""
    if template != "range_check":
        return True
    if params == ("max",):
        return p.get("min") is None
    if params == ("min",):
        return p.get("max") is None
    return True


def _spec(rule: dict) -> dict:
    spec = rule.get("rule_spec")
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except ValueError:
            spec = None
    return spec if isinstance(spec, dict) else {}


def _ir(rule: dict) -> dict:
    ir = _spec(rule).get("ir")
    return ir if isinstance(ir, dict) else {}


def _holds(rule: dict, template: str, params: tuple, field: str, value) -> bool:
    """Does this rule check `field` against exactly `value` with `template`?"""
    ir = _ir(rule)
    if ir.get("template") != template:
        return False
    p = ir.get("params") or {}
    if str(p.get("field") or "") != field or not _shape_fits(template, params, p):
        return False
    for key in params:
        have = p.get(key)
        if isinstance(have, list):
            want = value if isinstance(value, list) else [value]
            if sorted(str(x).strip().lower() for x in have) != \
                    sorted(str(x).strip().lower() for x in want):
                return False
        elif have is None or not _same(have, value):
            return False
    return True


def in_force(rule: dict, day: dt.date) -> bool:
    """A check still applying on `day` — undated, or dated over it."""
    return rw.covers(rw.window_of(rule), day)


def _norm(name) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def term_columns(key: str, fields: list[dict], limit_columns: dict,
                 find_column: Callable) -> list[str]:
    """The template columns a term's EXISTING checks may sit on.

    The column the term would be bound to today (contract_rules._find_column,
    which believes the template's data-model tags first), plus any column
    whose own NAME is one of the names the term goes by. The second matters on
    an uploaded contract: its checks were read from the PDF against the
    column's heading, so a template whose tags are wrong ("Commission Rate"
    tagged as a commission AMOUNT, three other columns tagged as the commission
    percent) still has the check on "Commission Rate" — and that is the check
    a change of commission must move. Finding a check is not binding a new
    one: a new check is still only ever bound through _find_column."""
    wanted = tuple(limit_columns.get(key) or ())
    out: list[str] = []
    column, _amb = find_column(wanted, fields) if wanted else (None, [])
    if column is not None:
        out.append(column["name"])
    names = {_norm(w) for w in wanted if _norm(w)}
    for f in fields:
        n = f.get("name")
        if n and _norm(n) in names and n not in out:
            out.append(n)
    return out


def current_terms_from_rules(rules: list[dict], fields: list[dict],
                             limits_spec: dict, limit_columns: dict,
                             limit_operators: dict, find_column: Callable,
                             day: dt.date) -> dict:
    """What an UPLOADED contract's terms are, read off the checks in force.

    An uploaded contract has no typed terms — its checks were read from the
    PDF. To change "Commission" on it, the panel needs to know the commission
    it is checked against now, and the plan needs the rule that checks it. A
    term is read only where exactly one check in force tests the term's own
    column with the comparison that term makes and one value; anything else is
    left blank rather than guessed."""
    out: dict = {}
    for key, spec in limits_spec.items():
        if not spec.get("check") or key not in limit_columns:
            continue
        columns = term_columns(key, fields, limit_columns, find_column)
        if not columns:
            continue
        op = limit_operators.get(key, "eq")
        found = []
        for r in rules:
            if not in_force(r, day) or r.get("rule_status") not in ("active", "disabled"):
                continue
            ir = _ir(r)
            p = ir.get("params") or {}
            if str(p.get("field") or "") not in columns:
                continue
            for template, params in _bounds(op, spec.get("kind") or ""):
                if ir.get("template") != template or not _shape_fits(template, params, p):
                    continue
                vals = [p.get(k) for k in params]
                if any(v is None for v in vals):
                    continue
                if len(params) == 2 and not _same(vals[0], vals[1]):
                    continue        # a range, not one agreed value
                v = vals[0]
                found.append(", ".join(str(x) for x in v) if isinstance(v, list)
                             else _show(v))
        if len(set(found)) == 1:
            out[key] = {"value": found[0], "read_from_checks": True}
    return out


def swap_value_text(text: Optional[str], old, new) -> Optional[str]:
    """`text` with every number equal to `old` written as `new` — "Commission
    rate is not 23.5%." → "… 25.5%.". Only numbers that ARE the old value move;
    a clause number or a year beside them stays."""
    if not text or _num(old) is None:
        return text
    o = _num(old)

    def rep(m):
        n = _num(m.group(0))
        return _show(new) if (n is not None and abs(n - o) < 1e-9) else m.group(0)
    return re.sub(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.]|\.\d)", rep, text)


def new_version(rule: dict, *, old, new, op: str, kind: str,
                compile_ir: Callable[[dict], Optional[str]]) -> Optional[dict]:
    """The new version of `rule` with the term's value moved, or None when the
    rule does not hold the value in a parameter this term moves.

    Everything else about the rule is kept — its template, its column, its
    severity, its sheets — and its query is compiled again by the same
    compiler, so it reads numbers exactly as the old version did and keeps its
    "must be a number" arm."""
    ir = copy.deepcopy(_ir(rule))
    p = ir.get("params") or {}
    moved = False
    for template, params in _bounds(op, kind):
        if ir.get("template") != template:
            continue
        for key in params:
            if key not in p:
                continue
            if isinstance(p[key], list):
                vals = new if isinstance(new, list) else \
                    [x.strip() for x in str(new).split(",") if x.strip()]
                p[key] = [str(x) for x in vals]
            else:
                n = _num(new)
                p[key] = n if n is not None else new
            moved = True
        break
    if not moved:
        return None
    ir["params"] = p
    # The name carries the value too ("Commission must be 25%") — a new
    # version named for the old value reads as the old check with a date on it.
    for k in ("rule_name", "error_message", "rule_description"):
        if ir.get(k):
            ir[k] = swap_value_text(ir[k], old, new)
    sql = compile_ir(ir)
    if not sql:
        return None
    spec = copy.deepcopy(_spec(rule))
    spec["ir"] = ir
    spec["compiled_sql"] = sql
    if "params" in spec:
        spec["params"] = p
    if spec.get("source") == "contract_terms" and "operand" in spec:
        spec["operand"] = p.get("max", p.get("min", p.get("allowed", new)))
    return {
        "rule_spec": spec,
        "rule_name": swap_value_text(rule.get("rule_name"), old, new),
        "error_message": swap_value_text(rule.get("error_message"), old, new),
        "rule_description": swap_value_text(rule.get("rule_description"), old, new),
    }


def rule_fingerprint(rule: dict) -> str:
    """What a rule checks and says. The go-live compares it with the preview's
    to tell whether a check moved in between."""
    payload = json.dumps({"spec": _spec(rule), "name": rule.get("rule_name"),
                          "msg": rule.get("error_message"),
                          "status": rule.get("rule_status"),
                          "to": str(rule.get("effective_to") or "")},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def plan_rules(*, changes: list[dict], rules: list[dict], fields_by_set: dict,
               effective: dt.date, limits_spec: dict, limit_columns: dict,
               limit_operators: dict, find_column: Callable,
               build_new: Callable[[str, dict, Any], Optional[dict]],
               compile_for_set: Callable[[Any], Callable[[dict], Optional[str]]],
               choices: Optional[dict] = None) -> dict:
    """Which checks a change of terms closes, which it opens, and what it
    cannot settle alone.

    `changes` are contract_wording.derive_endorsement_changes rows ({key, kind:
    added | amended | removed, from, to}) with `from`/`to` holding the RAW
    values (25.5, not the wording's "25.5%"). `rules` are the contract's checks
    (dicts with rule_id, rule_spec, rule_status, output_template_id,
    effective_from/_to, source_amendment_id). `fields_by_set` maps a rule set
    (its output_template_id, None for the contract's own) to that template's
    fields. `build_new(key, entry, set)` writes a check for a term that had
    none (contract_rules.map_limits_to_template for one term);
    `compile_for_set(set)` gives the compiler bound to that set's template.

    A term's checks are found by its key first (a contract written in Kavachio
    stores it on every term check) and otherwise by its column and its old
    value (an uploaded contract's checks were read from the PDF). In one rule
    set, two checks holding the same term is something nobody can resolve by
    reading — it goes to `needs_choice`, unless `choices[key]` names the rule
    ids to replace (an empty list: replace none, add a new check)."""
    choices = choices or {}
    close: list[dict] = []
    open_: list[dict] = []
    unchecked: list[dict] = []
    needs_choice: list[dict] = []
    live = [r for r in rules
            if in_force(r, effective) and r.get("rule_status") in ("active", "disabled")]

    for ch in changes:
        key, kind_ = ch.get("key"), ch.get("kind")
        spec = limits_spec.get(key) or {}
        if not spec.get("check"):
            continue                 # wording only — no check either side
        op = limit_operators.get(key, "eq")
        tkind = spec.get("kind") or ""
        old, new = ch.get("from"), ch.get("to")
        for set_id, fields in fields_by_set.items():
            in_set = [r for r in live if r.get("output_template_id") == set_id]
            by_key = [r for r in in_set if _spec(r).get("source") == "contract_terms"
                      and _spec(r).get("limit") == key]
            matched = by_key
            if not matched and old not in (None, "") and key in limit_columns:
                columns = term_columns(key, fields, limit_columns, find_column)
                matched = [r for r in in_set if any(
                    _holds(r, t, prm, col, old)
                    for col in columns for t, prm in _bounds(op, tkind))]
            if key in choices:
                picked = {int(x) for x in choices.get(key) or []}
                matched = [r for r in in_set if int(r["rule_id"]) in picked]
            elif len(matched) > 1:
                needs_choice.append({
                    "key": key, "question": spec.get("question") or key,
                    "set": set_id,
                    "candidates": [{"rule_id": r["rule_id"], "rule_name": r.get("rule_name"),
                                    "column": (_ir(r).get("params") or {}).get("field")}
                                   for r in matched]})
                continue

            if kind_ in ("amended", "removed"):
                for r in matched:
                    close.append({"rule_id": r["rule_id"], "rule_name": r.get("rule_name"),
                                  "key": key, "set": set_id,
                                  "fingerprint": rule_fingerprint(r),
                                  "from": _show(old) if old not in (None, "") else None})
            if kind_ in ("amended", "added") and new not in (None, ""):
                done = False
                for r in matched:
                    nv = new_version(r, old=old, new=new, op=op, kind=tkind,
                                     compile_ir=compile_for_set(set_id))
                    if nv is None:
                        continue
                    sev = ch.get("severity_to")
                    open_.append({"replaces": r["rule_id"], "key": key, "set": set_id,
                                  "rule_name": r.get("rule_name"),
                                  "rule_status": r.get("rule_status"),
                                  # a severity the carrier changed with the value
                                  "severity": sev if (sev and sev != ch.get("severity_from"))
                                  else None,
                                  "to": _show(new), **nv})
                    done = True
                if not done and not matched:
                    built = build_new(key, {"value": new, "severity": ch.get("severity_to")},
                                      set_id)
                    if built:
                        open_.append({"replaces": None, "key": key, "set": set_id,
                                      "to": _show(new), **built})
                    else:
                        unchecked.append({"key": key, "question": spec.get("question") or key,
                                          "set": set_id, "to": _show(new),
                                          "reason": "this template has no column that "
                                                    "measures it"})
                elif not done:
                    # A check holds the term but not in a parameter the term
                    # moves (a range from 20 to 30 for "commission is 25"):
                    # closing it alone would drop the check, so nothing moves
                    # and the carrier is told.
                    for r in matched:
                        close[:] = [c for c in close if c["rule_id"] != r["rule_id"]]
                        unchecked.append({"key": key, "question": spec.get("question") or key,
                                          "set": set_id, "to": _show(new),
                                          "rule_id": r["rule_id"],
                                          "reason": f"“{r.get('rule_name')}” checks it in a "
                                                    "way this change can't move by itself"})
    return {"close": close, "open": open_, "unchecked": unchecked,
            "needs_choice": needs_choice}


# ── clauses ─────────────────────────────────────────────────────────────────

CLAUSE_AMENDED, CLAUSE_ADDED, CLAUSE_REMOVED = "amended", "added", "removed"


def flat(text) -> str:
    """Text compared as words: line breaks and runs of spaces are layout."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clause_changes(current: list[dict], edits: list[dict]) -> tuple[list[dict], dict]:
    """The clause edits that change something, and {field: why} for the rest.

    `current` are the contract's clauses as they read today: {ref, title,
    text, section, clause_id}. `edits` are what the carrier sent: {ref,
    action: amended | added | removed, title, text, section}. A clause edited
    back to what it said is no change. A ref that is not one of this
    contract's clauses is refused rather than guessed at, and a new clause
    needs words — a heading alone puts nothing in the contract."""
    by_ref = {c["ref"]: c for c in current or []}
    out: list[dict] = []
    errors: dict = {}
    seen: set = set()
    for i, e in enumerate(edits or []):
        if not isinstance(e, dict):
            continue
        action = str(e.get("action") or "").strip()
        title = flat(e.get("title"))
        text = str(e.get("text") or "").strip()
        field = f"clause_{i}"
        if action == CLAUSE_ADDED:
            if not flat(text):
                errors[field] = (f"Write the new clause{f' “{title}”' if title else ''}, "
                                 "or take it out.")
                continue
            out.append({"index": i, "ref": None, "action": CLAUSE_ADDED,
                        "clause_id": None, "title": title or None,
                        "section": flat(e.get("section")) or None,
                        "from_title": None, "from_text": None, "to_text": text})
            continue
        cur = by_ref.get(e.get("ref"))
        if cur is None:
            errors[field] = "That clause is not one of this contract's clauses."
            continue
        if cur["ref"] in seen:
            errors[field] = f"“{cur.get('title') or 'This clause'}” is changed twice."
            continue
        seen.add(cur["ref"])
        base = {"index": i, "ref": cur["ref"], "clause_id": cur.get("clause_id"),
                "kind": cur.get("kind"),
                "section": cur.get("section"), "from_title": cur.get("title"),
                "from_text": cur.get("text")}
        if action == CLAUSE_REMOVED:
            out.append({**base, "action": CLAUSE_REMOVED, "title": cur.get("title"),
                        "to_text": None})
        elif action == CLAUSE_AMENDED:
            same_words = flat(text) == flat(cur.get("text"))
            same_title = not title or title == flat(cur.get("title"))
            if same_words and same_title:
                continue
            if not flat(text):
                errors[field] = (f"“{cur.get('title') or 'This clause'}” has no words left — "
                                 "delete the clause instead.")
                continue
            out.append({**base, "action": CLAUSE_AMENDED,
                        "title": title or cur.get("title"), "to_text": text})
        else:
            errors[field] = "Say whether the clause is amended, added or deleted."
    return out, errors


def clause_change_line(ch: dict) -> str:
    """One clause change in a sentence — the history, the email, the card."""
    name = ch.get("from_title") or ch.get("title") or "a clause"
    if ch["action"] == CLAUSE_ADDED:
        return f"New clause “{ch.get('title') or 'Untitled'}” added"
    if ch["action"] == CLAUSE_REMOVED:
        return f"Clause “{name}” deleted"
    return f"Clause “{name}” re-worded"


def swap_value_in_clause(text: Optional[str], old, new) -> Optional[str]:
    """A clause's wording with the term's old value written as the new one —
    "8) Commissions Schedule: 23.5%" → "… 25.5%", "$3,000,000 per policy" →
    "$3,500,000 per policy". Only numbers that ARE the old value move (a
    clause number or a date beside it stays), and a value written with
    thousands separators is rewritten with them. None when the old value does
    not appear in it — the clause then states the term some other way, and
    only the carrier can re-word it."""
    o = _num(old)
    if not text or o is None or _num(new) is None:
        return None
    hit = False

    def rep(m):
        nonlocal hit
        raw = m.group(0)
        n = _num(raw)
        if n is None or abs(n - o) >= 1e-9:
            return raw
        # "8)" or "8." opening a line numbers the clause; it is not a value
        line_start = text.rfind("\n", 0, m.start()) + 1
        if not text[line_start:m.start()].strip() and text[m.end():m.end() + 1] in (")", "."):
            return raw
        hit = True
        shown = _show(new)
        if "," in raw:
            whole, _, frac = shown.partition(".")
            shown = f"{int(whole):,}" + (f".{frac}" if frac else "")
        return shown
    out = re.sub(r"(?<![\w.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\w]|\.\d|,\d)"
                 r"|(?<![\w.,])\d+(?:\.\d+)?(?![\w.]|\.\d|,\d)", rep, text)
    return out if hit else None

