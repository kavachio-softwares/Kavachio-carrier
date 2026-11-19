"""
programme_rules.py
──────────────────
Programme Rules: checks the CARRIER sets once for a whole programme, that apply
to every broker contract and/or reinsurance treaty on it (or the ones it picks),
and that start checking for a broker or a reinsurer once that party approves.

WHAT A RULE IS
  One of the questions a contract's agreed limits ask, with its answer —
  "Biggest risk you will cover: 5,000,000", "Where business may not be
  written: Cuba, Iran" — or "Policy number must be filled in"; with a
  severity (CHECKS). Not tied to a column, so one rule fits every bordereau
  setup on the programme: when a file is checked the question's column is
  found in that setup's own output template the way a contract term's is
  (contract_rules.LIMIT_COLUMNS, _find_column) and compiled there (_ir_for,
  _compile). A template with no such column cannot be checked against it, and
  the run says so in words. (pr_field holds the question's key; a rule saved
  before the questions held a data-model key, and still reads.)

  On a treaty the same rule becomes one of the treaty rule kinds
  (treaty_rules: at most → max, is one of → allowed, is not one of →
  excluded, must be filled → required) on the subject the field is read from
  (treaty_rules.SUBJECT_FIELDS), and checks the reinsurance statements. A rule
  whose field or condition a treaty cannot check is refused for treaties.

WHO APPROVES
  `approval_from` — broker, reinsurer or both. The brokers asked are those of
  the broker contracts the rule covers; the reinsurers, every reinsurer on the
  panel of the treaties it covers. A side not asked needs no approval: the rule
  applies to it as soon as it is sent.

WHEN IT CHECKS (per party)
  A rule applies to a broker contract once the broker of that contract has
  approved this version, and to a treaty once every reinsurer on its panel has.
  A contract on a side that needs no approval is checked once the rule is sent.

CHANGES
  Editing a rule that was sent makes it a draft again with the next version
  number; sending it again asks every approver afresh. Earlier answers stay as
  history and never count for a later version. A retired rule checks nothing.

STORAGE (migration 48)
  programme_rule            the rule, the contracts it covers, its version
  programme_rule_approval   one row per approver per version
  Their own pair rather than validation_rule: a rule there with no contract is
  read by validation_routes as the carrier's default for EVERY contract, before
  anyone approved it; and contract_approval / reinsurer_approval each belong to
  one contract. Kept on their own model registry (RuleBase), so db.init_db's
  create_all never builds them on a database by itself: a person applies
  migration 48.

Every function takes the caller's session; reading never writes.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import JSON, Column, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import declarative_base

log = logging.getLogger("kavachio.programme_rules")

RuleBase = declarative_base()

DRAFT, SENT, RETIRED = "draft", "sent", "retired"
PENDING, APPROVED, CHANGES = "pending", "approved", "changes_requested"
BROKER, REINSURER = "broker", "reinsurer"
SCOPES = ("none", "all", "selected")
APPROVERS = ("broker", "reinsurer", "both")
SEVERITIES = ("critical", "warning", "info")
ENDED = ("terminated", "superseded", "expired")

# The conditions, as the screen says them.
OPERATORS: dict[str, str] = {
    "lte": "is at most", "gte": "is at least", "eq": "equals",
    "in": "is one of", "not_in": "is not one of",
    "lte_aggregate": "total is at most", "required": "must be filled",
}
_LISTS = {"in", "not_in"}
_NUMBERS = {"lte", "gte", "lte_aggregate"}
# A data-model type → contract_rules' kind (whether "equals" compares numbers).
_KIND = {"decimal": "money", "int": "int"}
# What each condition becomes on a treaty (treaty_rules.KINDS).
TREATY_KIND = {"lte": "max", "in": "allowed", "not_in": "excluded", "required": "required"}

# ── what a rule can check, in the carrier's words ──────────────────────────
# The same questions a contract's agreed limits ask (contract_types.
# AGREED_LIMITS), found in a bordereau the same way (contract_rules.
# LIMIT_COLUMNS) and compared the same way (contract_rules.LIMIT_OPERATORS) —
# so a programme rule checks a file exactly as that term on a contract would.
# `treaty`: the treaty rule kind and subject it becomes on a reinsurance
# statement, or None where a treaty cannot check it.
_LIMIT_CHECKS: list[tuple[str, str, Optional[tuple[str, str]]]] = [
    ("max_sum_insured", "Limits", ("max", "sum_insured")),
    ("max_tiv", "Limits", ("max", "sum_insured")),
    ("min_premium", "Limits", None),
    ("premium_cap_total", "Limits", None),
    ("deductible", "Limits", None),
    ("commission_max_pct", "Limits", None),
    ("territory", "Where and what", ("allowed", "territory")),
    ("excluded_territory", "Where and what", ("excluded", "territory")),
    ("permitted_risks", "Where and what", ("allowed", "class_of_business")),
    ("excluded_risks", "Where and what", ("excluded", "class_of_business")),
    ("coverage", "Where and what", None),
    ("currency", "Other", ("currency", "currency")),
    ("policy_period_months", "Other", None),
]
# "Must be filled in on every row": (key, label, the columns it is looked for
# in — data-model key first, then names a template may carry — treaty subject).
_REQUIRED_CHECKS: list[tuple[str, str, tuple[str, ...], Optional[str]]] = [
    ("policy_number", "Policy number", ("policy_number", "policy_no", "policy_reference"), "policy"),
    ("policy_start", "Policy start date", ("policy_effective_date", "inception_date", "policy_start_date"), None),
    ("insured_name", "Insured name", ("policyholder_legal_name", "insured_name", "insured"), None),
    ("gross_premium", "Gross premium", ("premium_transaction_gross_premium_amount", "gross_premium",
                                        "gross_written_premium"), "premium"),
    ("sum_insured", "Sum insured", ("policy_sum_insured_amount", "sum_insured"), "sum_insured"),
    ("country", "Country of the risk", ("risk_location_country", "policy_risk_country", "risk_country"), None),
]
_VALUE = {"money": "amount", "percent": "percent", "int": "months", "text": "text"}


def _catalog() -> dict[str, dict]:
    import contract_rules as cr
    from contract_types import AGREED_LIMITS
    out: dict[str, dict] = {}
    for key, group, treaty in _LIMIT_CHECKS:
        spec, op = AGREED_LIMITS[key], cr.LIMIT_OPERATORS[key]
        out[key] = {"key": key, "label": spec["question"], "group": group, "operator": op,
                    "value": "list" if op in _LISTS else _VALUE.get(spec["kind"], "text"),
                    "kind": spec["kind"], "columns": cr.LIMIT_COLUMNS[key], "treaty": treaty}
    for key, label, cols, subj in _REQUIRED_CHECKS:
        out[f"required:{key}"] = {"key": f"required:{key}", "label": f"{label} must be filled in",
                                  "group": "Must be filled in on every row", "operator": "required",
                                  "value": None, "kind": "text", "columns": cols,
                                  "treaty": ("required", subj) if subj else None}
    return out


CHECKS = _catalog()


def check_of(field: str, operator: str) -> Optional[str]:
    """The question a rule asks: its own key, or — for a rule saved before the
    questions, which held a data-model field — the question that reads that
    field the same way. None when there is no such question."""
    if field in CHECKS:
        return field
    return next((k for k, c in CHECKS.items()
                 if c["operator"] == operator and field in c["columns"]), None)


def checks() -> list[dict]:
    """The questions a rule can ask, for the form, in order."""
    return [{k: c[k] for k in ("key", "label", "group", "value")} | {"treaty": c["treaty"] is not None}
            for c in CHECKS.values()]


class ProgrammeRule(RuleBase):
    __tablename__ = "programme_rule"
    id = Column("pr_id", Integer, primary_key=True)
    tenant_id = Column("pr_tenant_id", Integer, nullable=False, index=True)
    program_id = Column("pr_program_id", Integer, nullable=False, index=True)
    name = Column("pr_name", String(200), nullable=False)
    description = Column("pr_description", Text, nullable=True)
    field = Column("pr_field", String(120), nullable=False)          # data_model key
    operator = Column("pr_operator", String(20), nullable=False)     # OPERATORS
    operand = Column("pr_operand", JSON, nullable=True)              # number | text | [text]
    severity = Column("pr_severity", String(10), nullable=False, default="warning")
    broker_scope = Column("pr_broker_scope", String(10), nullable=False, default="all")
    broker_contract_ids = Column("pr_broker_contract_ids", JSON, nullable=True)
    treaty_scope = Column("pr_treaty_scope", String(10), nullable=False, default="none")
    treaty_contract_ids = Column("pr_treaty_contract_ids", JSON, nullable=True)
    approval_from = Column("pr_approval_from", String(10), nullable=False, default="broker")
    status = Column("pr_status", String(12), nullable=False, default=DRAFT)
    version = Column("pr_version", Integer, nullable=False, default=1)
    created_by_user_id = Column("pr_created_by_user_id", Integer, nullable=True)
    created_at = Column("pr_created_at", DateTime, default=dt.datetime.utcnow)
    updated_at = Column("pr_updated_at", DateTime, default=dt.datetime.utcnow)
    sent_at = Column("pr_sent_at", DateTime, nullable=True)


class ProgrammeRuleApproval(RuleBase):
    __tablename__ = "programme_rule_approval"
    __table_args__ = (Index("uq_programme_rule_approval", "pra_rule_id", "pra_version",
                            "pra_party_kind", "pra_party_id", unique=True),)
    id = Column("pra_id", Integer, primary_key=True)
    rule_id = Column("pra_rule_id", Integer, nullable=False, index=True)
    tenant_id = Column("pra_tenant_id", Integer, nullable=False, index=True)
    version = Column("pra_version", Integer, nullable=False)
    party_kind = Column("pra_party_kind", String(10), nullable=False)   # broker | reinsurer
    party_id = Column("pra_party_id", Integer, nullable=False, index=True)
    status = Column("pra_status", String(20), nullable=False, default=PENDING)
    note = Column("pra_note", Text, nullable=True)
    acted_by_user_id = Column("pra_acted_by_user_id", Integer, nullable=True)
    acted_by_name = Column("pra_acted_by_name", String(200), nullable=True)
    acted_at = Column("pra_acted_at", DateTime, nullable=True)
    created_at = Column("pra_created_at", DateTime, default=dt.datetime.utcnow)


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


def missing_tables(s) -> bool:
    """True when migration 48 has not been applied to this database."""
    from sqlalchemy import inspect
    insp = inspect(s.connection())        # the session's own: never resets its work
    return not (insp.has_table("programme_rule") and insp.has_table("programme_rule_approval"))


NOT_SET_UP = ("Programme Rules are not set up on this database yet. "
              "Ask for migration 48 to be applied.")


def require_tables(s) -> None:
    # 409, not 5xx: the app shows a 5xx as a generic "went wrong" popup, and
    # this is a known, explainable state.
    if missing_tables(s):
        raise HTTPException(409, NOT_SET_UP)


# ── the fields a rule can be written against ───────────────────────────────
def treaty_subject(field: str) -> Optional[str]:
    """The treaty rule subject a data-model field is read as, or None."""
    import treaty_rules as trr
    return next((subj for subj, fields in trr.SUBJECT_FIELDS.items() if field in fields), None)


def _field_type(key: str) -> Optional[str]:
    import data_model as dm
    v = dm.DATA_MODEL.get(key)
    return v["type"] if v and v.get("source") == "bdx" else None


def treaty_ready(field: str, operator: str) -> bool:
    """Whether a treaty can check this rule (a treaty rule kind and subject)."""
    if field in CHECKS:
        return CHECKS[field]["treaty"] is not None
    subj = treaty_subject(field)
    if operator not in TREATY_KIND or subj is None:
        return False
    import treaty_rules as trr
    # "at most" compares an amount: only on the subjects that are amounts.
    return operator != "lte" or subj in trr.NUMERIC


# ── what a rule covers ─────────────────────────────────────────────────────
def is_treaty(c) -> bool:
    import programme_report as pr
    return not pr._with_broker(c)


def broker_contracts(s, tenant_id: int, program_id: int) -> list:
    """The programme's broker contracts that have not ended."""
    from db import Contract
    return [c for c in s.query(Contract).filter(Contract.tenant_id == tenant_id,
                                                Contract.program_id == program_id)
            .order_by(Contract.id)
            if c.broker_party_id and not is_treaty(c)
            and (c.lifecycle or "").strip().lower() not in ENDED]


def treaties(s, tenant_id: int, program_id: int) -> list:
    """The treaties protecting the programme (its home and any it covers)."""
    from db import Contract, TreatyProgramme
    ids = {r[0] for r in s.query(TreatyProgramme.contract_id)
           .filter(TreatyProgramme.program_id == program_id)}
    q = s.query(Contract).filter(Contract.tenant_id == tenant_id)
    found = q.filter(Contract.id.in_(ids)).all() if ids else []
    found += [c for c in q.filter(Contract.program_id == program_id) if c.id not in ids]
    return sorted([c for c in found if is_treaty(c)
                   and (c.lifecycle or "").strip().lower() not in ENDED], key=lambda c: c.id)


def covered(rule: ProgrammeRule, contracts: list, side: str) -> list:
    """The contracts of one side (broker | reinsurer) the rule covers."""
    scope, picked = ((rule.broker_scope, rule.broker_contract_ids) if side == BROKER
                     else (rule.treaty_scope, rule.treaty_contract_ids))
    if scope == "all":
        return list(contracts)
    if scope == "selected":
        want = {int(x) for x in picked or []}
        return [c for c in contracts if c.id in want]
    return []


def panel(s, c) -> set[int]:
    """The reinsurers on a treaty (treaty_routes' panel)."""
    import treaty_routes as tr
    return tr._panel_party_ids(s, c)


def asks(rule: ProgrammeRule, side: str) -> bool:
    return rule.approval_from in (side, "both")


def approvers(s, rule: ProgrammeRule) -> list[tuple[str, int]]:
    """(kind, party_id) of everybody this version needs an answer from."""
    out: list[tuple[str, int]] = []
    if asks(rule, BROKER):
        bc = covered(rule, broker_contracts(s, rule.tenant_id, rule.program_id), BROKER)
        out += [(BROKER, pid) for pid in sorted({c.broker_party_id for c in bc})]
    if asks(rule, REINSURER):
        tc = covered(rule, treaties(s, rule.tenant_id, rule.program_id), REINSURER)
        out += [(REINSURER, pid) for pid in sorted({p for c in tc for p in panel(s, c)})]
    return out


def answers(s, rule: ProgrammeRule) -> dict[tuple[str, int], ProgrammeRuleApproval]:
    """This version's answer rows, by (kind, party)."""
    return {(a.party_kind, a.party_id): a for a in
            s.query(ProgrammeRuleApproval).filter(ProgrammeRuleApproval.rule_id == rule.id,
                                                  ProgrammeRuleApproval.version == rule.version)}


def applies_to(s, rule: ProgrammeRule, c, got: Optional[dict] = None) -> bool:
    """Whether the rule checks this contract now (see WHEN IT CHECKS)."""
    if rule.status != SENT:
        return False
    side = REINSURER if is_treaty(c) else BROKER
    pool = (treaties if side == REINSURER else broker_contracts)(s, rule.tenant_id, rule.program_id)
    if c.id not in {x.id for x in covered(rule, pool, side)}:
        return False
    if not asks(rule, side):
        return True
    got = got if got is not None else answers(s, rule)
    parties = panel(s, c) if side == REINSURER else {c.broker_party_id}
    return bool(parties) and all(
        (a := got.get((side, pid))) is not None and a.status == APPROVED for pid in parties)


# ── writing ────────────────────────────────────────────────────────────────
def _clean_operand(field: str, op: str, value: Any) -> Any:
    if op == "required":
        return None
    if op not in _LISTS and op != "eq" and CHECKS.get(field, {}).get("value") in ("amount", "percent", "months"):
        op = "lte"                         # a number, whatever its comparison
    if op in _LISTS:
        import contract_rules as cr
        items = value if isinstance(value, list) else cr._split_list(value or "")
        items = [str(x).strip() for x in items if str(x).strip()]
        if not items:
            raise HTTPException(400, "List at least one value.")
        return items
    if value in (None, ""):
        raise HTTPException(400, "Enter a value.")
    if op in _NUMBERS or (op == "eq" and _field_type(field) in ("decimal", "int")):
        if str(value).strip().endswith("%"):
            value = str(value).strip()[:-1]
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            raise HTTPException(400, "Enter a number.")
    return str(value).strip()


def clean(s, tenant_id: int, program_id: int, body: dict) -> dict:
    """The rule as the screen sent it, checked; plain words when it cannot be saved."""
    field = (body.get("check") or body.get("field") or "").strip()
    check = CHECKS.get(field)
    if check is None:
        raise HTTPException(400, "Pick what the rule should check.")
    op = check["operator"]
    # A rule with no name is called what it checks.
    name = (body.get("name") or "").strip() or check["label"]
    sev = body.get("severity") or "warning"
    if sev not in SEVERITIES:
        raise HTTPException(400, "Pick how serious it is.")
    bscope, tscope = body.get("broker_scope") or "none", body.get("treaty_scope") or "none"
    if bscope not in SCOPES or tscope not in SCOPES:
        raise HTTPException(400, "Pick the contracts it applies to.")
    if bscope == "none" and tscope == "none":
        raise HTTPException(400, "Pick broker contracts, treaties or both.")
    appr = body.get("approval_from")
    if appr not in APPROVERS:
        raise HTTPException(400, "Pick who approves it.")
    bids = [int(x) for x in body.get("broker_contract_ids") or []] if bscope == "selected" else []
    tids = [int(x) for x in body.get("treaty_contract_ids") or []] if tscope == "selected" else []
    own_b = {c.id for c in broker_contracts(s, tenant_id, program_id)}
    own_t = {c.id for c in treaties(s, tenant_id, program_id)}
    if bscope == "selected" and (not bids or not set(bids) <= own_b):
        raise HTTPException(400, "Pick at least one of this programme's broker contracts.")
    if tscope == "selected" and (not tids or not set(tids) <= own_t):
        raise HTTPException(400, "Pick at least one of this programme's treaties.")
    if tscope != "none" and not treaty_ready(field, op):
        raise HTTPException(400, "This can only be checked on broker contracts, not on "
                                 "reinsurance treaties.")
    if appr in ("broker", "both") and bscope == "none":
        raise HTTPException(400, "Brokers can only approve a rule that applies to broker contracts.")
    if appr in ("reinsurer", "both") and tscope == "none":
        raise HTTPException(400, "Reinsurers can only approve a rule that applies to treaties.")
    return {"name": name[:200], "description": (body.get("description") or "").strip() or None,
            "field": field, "operator": op, "operand": _clean_operand(field, op, body.get("operand")),
            "severity": sev, "broker_scope": bscope, "broker_contract_ids": bids,
            "treaty_scope": tscope, "treaty_contract_ids": tids, "approval_from": appr}


def create(s, tenant_id: int, program_id: int, body: dict, user_id: Optional[int]) -> ProgrammeRule:
    r = ProgrammeRule(tenant_id=tenant_id, program_id=program_id, status=DRAFT, version=1,
                      created_by_user_id=user_id, created_at=_now(), updated_at=_now(),
                      **clean(s, tenant_id, program_id, body))
    s.add(r)
    s.flush()
    return r


def update(s, rule: ProgrammeRule, body: dict) -> ProgrammeRule:
    """An edit. A rule that was sent becomes a draft of its next version."""
    if rule.status == RETIRED:
        raise HTTPException(409, "This rule is retired. Add a new one instead.")
    for k, v in clean(s, rule.tenant_id, rule.program_id, body).items():
        setattr(rule, k, v)
    if rule.status == SENT:
        rule.version += 1
        rule.status = DRAFT
    rule.updated_at = _now()
    return rule


def send(s, rule: ProgrammeRule) -> list[tuple[str, int]]:
    """Send this version: it is live for every side that needs no approval,
    and an answer is asked of every approver. Returns those newly asked."""
    if rule.status == RETIRED:
        raise HTTPException(409, "This rule is retired.")
    who = approvers(s, rule)
    if not who and rule.approval_from != "none":
        raise HTTPException(409, "Nobody can approve this rule: the contracts it covers "
                                 "have no broker or reinsurer yet.")
    have = answers(s, rule)
    new = [(k, pid) for k, pid in who if (k, pid) not in have]
    for k, pid in new:
        s.add(ProgrammeRuleApproval(rule_id=rule.id, tenant_id=rule.tenant_id, version=rule.version,
                                    party_kind=k, party_id=pid, status=PENDING, created_at=_now()))
    rule.status, rule.sent_at, rule.updated_at = SENT, _now(), _now()
    return new


def retire(rule: ProgrammeRule) -> None:
    rule.status, rule.updated_at = RETIRED, _now()


def answer(s, rule: ProgrammeRule, kind: str, party_id: int, status: str, *,
           note: Optional[str] = None, user_id: Optional[int] = None,
           name: Optional[str] = None) -> ProgrammeRuleApproval:
    """A broker's or reinsurer's answer to this version."""
    if rule.status != SENT:
        raise HTTPException(409, "This rule is no longer waiting for an answer.")
    a = answers(s, rule).get((kind, party_id))
    if a is None:
        raise HTTPException(404, "This rule was not sent to you.")
    if status == CHANGES and not (note or "").strip():
        raise HTTPException(400, "Say what should change.")
    a.status, a.note = status, (note or "").strip() or None
    a.acted_by_user_id, a.acted_by_name, a.acted_at = user_id, name, _now()
    return a


# ── checking ───────────────────────────────────────────────────────────────
def sentence(rule: ProgrammeRule) -> str:
    """"Biggest risk you will cover: 5,000,000" / "Policy number must be filled in"."""
    v = rule.operand
    if isinstance(v, list):
        v = ", ".join(v)
    elif isinstance(v, float):
        v = f"{v:,.0f}" if v == int(v) else f"{v:,}"
    check = CHECKS.get(check_of(rule.field, rule.operator) or "")
    if check is not None:
        if rule.operator == "required":
            return check["label"]
        unit = {"percent": "%", "months": " months"}.get(check["value"], "")
        return f"{check['label']}: {v}{unit}"
    what = rule.field.replace("_", " ")
    return f"{what} {OPERATORS[rule.operator]}" + (f" {v}" if rule.operator != "required" else "")


def _ir(rule: ProgrammeRule, column: str) -> Optional[dict]:
    import contract_rules as cr
    if rule.operator == "required":
        return {"template": "required_field", "params": {"field": column}, "rule_name": rule.name}
    key = check_of(rule.field, rule.operator)
    kind = CHECKS[key]["kind"] if key else _KIND.get(_field_type(rule.field) or "", "text")
    return cr._ir_for(op=rule.operator, kind=kind, field=column, operand=rule.operand,
                      rule_name=rule.name)


def for_broker_run(s, tenant_id: int, governing: list, structure: Optional[dict]) -> list[dict]:
    """The programme rules that check this file: those applying to any of its
    governing contracts, compiled against the setup's own output template.
    Shaped as validation_rule rows for duckdb_validation.run_validation; ids
    are negative (never a validation_rule id) and the SQL is not cached.
    None while Phase 2 is switched off (phase2.py): programme rules are a
    Phase 2 feature, so they check nothing then — approved ones included."""
    from phase2 import phase2_enabled
    if not phase2_enabled() or not governing or missing_tables(s):
        return []
    import contract_rules as cr
    from app_routes import _template_fields_from_structure
    programmes = {c.program_id for c in governing if c.program_id}
    rules = (s.query(ProgrammeRule).filter(ProgrammeRule.tenant_id == tenant_id,
                                           ProgrammeRule.program_id.in_(programmes),
                                           ProgrammeRule.status == SENT).all()
             if programmes else [])
    if not rules:
        return []
    template_fields = _template_fields_from_structure(structure or {})
    out = []
    for r in rules:
        got = answers(s, r)
        hit = next((c for c in governing if c.program_id == r.program_id and applies_to(s, r, c, got)), None)
        if hit is None:
            continue
        key = check_of(r.field, r.operator)
        wanted = CHECKS[key]["columns"] if key else (r.field,)
        column, _amb = cr._find_column(wanted, template_fields)
        ir = _ir(r, column["name"]) if column else None
        sql = cr._compile(ir, column["name"], template_fields, column.get("sheet")) if ir else None
        out.append({
            "rule_id": -r.id, "contract_id": hit.id, "rule_engine": "custom",
            "rule_name": f"Programme rule: {r.name}", "severity": r.severity,
            "canonical_target": {"output_field": column["name"] if column else None,
                                 "sheet": column.get("sheet") if column else None,
                                 "canonical_field": r.field},
            "rule_spec": {"source": "programme", "programme_rule_id": r.id, "version": r.version,
                          "ir": ir, "compiled_sql": sql},
            "error_message": (f"{column['name']} breaks the programme rule “{r.name}” "
                              f"({sentence(r)})." if column else None),
            # Said when the setup's template has no column for it.
            "no_column": None if column else
                f"This bordereau has no column for “{sentence(r)}”, so it could not be checked.",
            "no_cache": True,
        })
    return out


def for_treaty(s, c) -> list[dict]:
    """The programme rules that check this treaty's statements, as treaty rules."""
    import treaty_rules as trr
    from db import TreatyProgramme
    from phase2 import phase2_enabled
    if not phase2_enabled() or missing_tables(s):
        return []
    progs = {r[0] for r in s.query(TreatyProgramme.program_id)
             .filter(TreatyProgramme.contract_id == c.id)} | ({c.program_id} if c.program_id else set())
    if not progs:
        return []
    out = []
    for r in s.query(ProgrammeRule).filter(ProgrammeRule.tenant_id == c.tenant_id,
                                           ProgrammeRule.program_id.in_(progs),
                                           ProgrammeRule.status == SENT):
        key = check_of(r.field, r.operator)
        if not treaty_ready(key or r.field, r.operator) or not applies_to(s, r, c):
            continue
        kind, subj = (CHECKS[key]["treaty"] if key
                      else (TREATY_KIND[r.operator], treaty_subject(r.field)))
        params = ({"value": r.operand} if kind in ("max", "currency") else
                  {"values": list(r.operand or [])} if kind in ("allowed", "excluded") else {})
        out.append(trr._rule(f"programme_{r.id}", f"Programme rule: {r.name}", kind, subj,
                             severity="warning" if r.severity != "critical" else "critical",
                             source="programme", clause=sentence(r), **params))
    return out


# ── views ──────────────────────────────────────────────────────────────────
def _names(s, ids: set[int]) -> dict[int, str]:
    from db import Party
    return dict(s.query(Party.id, Party.legal_name).filter(Party.id.in_(ids)).all()) if ids else {}


def status_line(rule: ProgrammeRule, rows: list[dict]) -> str:
    """"Draft" / "Waiting for approval (2 of 3)" / "Changes requested by …" / "Active" / "Retired"."""
    if rule.status == DRAFT:
        return "Draft" if rule.version == 1 else f"Draft (version {rule.version})"
    if rule.status == RETIRED:
        return "Retired"
    asked = [x for x in rows]
    if not asked:
        return "Active"
    back = [x["party"] for x in asked if x["status"] == CHANGES]
    if back:
        return "Changes requested by " + ", ".join(back)
    yes = sum(1 for x in asked if x["status"] == APPROVED)
    return "Active" if yes == len(asked) else f"Waiting for approval ({yes} of {len(asked)})"


def view(s, rule: ProgrammeRule, names: Optional[dict] = None) -> dict:
    got = answers(s, rule)
    names = names if names is not None else _names(s, {pid for _k, pid in got})
    rows = [{"kind": a.party_kind, "party_id": a.party_id,
             "party": names.get(a.party_id) or f"#{a.party_id}", "status": a.status,
             "note": a.note, "by": a.acted_by_name,
             "at": a.acted_at.isoformat() + "Z" if a.acted_at else None}
            for a in sorted(got.values(), key=lambda a: (a.party_kind, a.party_id))]
    return {"id": rule.id, "program_id": rule.program_id, "name": rule.name,
            "description": rule.description,
            "check": check_of(rule.field, rule.operator) or rule.field, "operator": rule.operator,
            "operand": rule.operand, "severity": rule.severity, "sentence": sentence(rule),
            "broker_scope": rule.broker_scope, "broker_contract_ids": rule.broker_contract_ids or [],
            "treaty_scope": rule.treaty_scope, "treaty_contract_ids": rule.treaty_contract_ids or [],
            "approval_from": rule.approval_from, "status": rule.status, "version": rule.version,
            "status_line": status_line(rule, rows), "approvals": rows,
            "sent_at": rule.sent_at.isoformat() + "Z" if rule.sent_at else None,
            "updated_at": rule.updated_at.isoformat() + "Z" if rule.updated_at else None}


def contract_choices(s, tenant_id: int, program_id: int) -> dict:
    """The broker contracts and treaties a rule can be pointed at."""
    bc, tc = broker_contracts(s, tenant_id, program_id), treaties(s, tenant_id, program_id)
    names = _names(s, {c.broker_party_id for c in bc if c.broker_party_id}
                   | {p for c in tc for p in panel(s, c)})
    return {"broker": [{"id": c.id, "name": c.name or c.filename or f"Contract #{c.id}",
                        "party": names.get(c.broker_party_id) or "—"} for c in bc],
            "treaty": [{"id": c.id, "name": c.name or f"Treaty #{c.id}",
                        "party": ", ".join(sorted(names.get(p) or f"#{p}" for p in panel(s, c))) or "—"}
                       for c in tc]}


def asked_of(s, kind: str, party_id: int, tenant_ids: Optional[set] = None) -> list[tuple]:
    """(rule, its answer row) for every live rule this party was asked to approve."""
    q = (s.query(ProgrammeRule, ProgrammeRuleApproval)
         .join(ProgrammeRuleApproval, ProgrammeRuleApproval.rule_id == ProgrammeRule.id)
         .filter(ProgrammeRuleApproval.party_kind == kind, ProgrammeRuleApproval.party_id == party_id,
                 ProgrammeRuleApproval.version == ProgrammeRule.version,
                 ProgrammeRule.status == SENT))
    if tenant_ids is not None:
        q = q.filter(ProgrammeRule.tenant_id.in_(tenant_ids or {0}))
    return q.order_by(ProgrammeRule.id.desc()).all()
