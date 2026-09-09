"""
contract_rules.py
─────────────────
The agreed limits of a contract, turned into the checks that run on a file.

WHERE THIS SITS. A contract arriving in Kavachio is read for its CLAUSES and
nothing more — a rule is written against a bordereau column, and until a
bordereau template is chosen there are no columns to write against. Mapping is
therefore a separate, later step, done when the BDX setup binds this contract to
an output template. That is the moment the columns become known, and it is the
moment this module runs.

WHAT MAKES THIS DIFFERENT FROM EXTRACTION. For a contract WRITTEN in Kavachio
there is nothing to interpret. The carrier said "commission may not exceed 15%"
by typing 15 into a row that already knows it means `commission_pct <= {v}` and
already carries the severity they chose. Turning that into a rule is a
translation, not an inference: no model call, no confidence score, and the same
input gives the same rules every time. An uploaded PDF still needs the
extraction pipeline, because the sentence has to be understood before it can be
mapped; this path exists for the contracts where that work was never necessary.

WHAT IT REFUSES TO GUESS. A limit whose column is not in the chosen template
produces NO rule, and is reported instead. "Deductible must be at least 25,000"
cannot be checked by a template with no deductible column, and a rule bound to
the nearest-looking column would fail rows for the wrong reason — which is worse
than not checking, because it is wrong in a way people act on.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from contract_types import AGREED_LIMITS

log = logging.getLogger("kavachio.contracts.rules")

# Which canonical column each limit is checked against. The names on the right
# are the DATA MODEL's own keys (data_model.DATA_MODEL), matched against a
# template column's `canonical_field` first and its literal name second — a
# template mapped to the data model gives an exact hit, and one that was never
# mapped can still match by column name.
#
# THE CANONICAL KEY COMES FIRST AND IT IS THE REAL ONE. This list used to hold
# short, plausible names — `sum_insured`, `risk_country`, `commission_pct` —
# that no template has ever carried, because the data model spells them
# `policy_sum_insured_amount`, `risk_location_country` and
# `premium_transaction_commission_percent`. Every limit therefore fell through
# to the by-name fallback and matched only where a column happened to be
# LABELLED the way this file guessed, which on a real Lloyd's-shaped bordereau
# was one limit in eight: the contract said eight things and one of them was
# ever measured. The shorter names are kept after the canonical ones, because
# they still match a column somebody named that way by hand.
#
# A limit absent from here has no column to check and is recorded as wording
# only; see AGREED_LIMITS, where `check` is None for exactly those.
# test_contract_checks holds every canonical name here to the data model, so
# this cannot drift back into naming columns that do not exist.
LIMIT_COLUMNS: dict[str, tuple[str, ...]] = {
    # cover
    "coverage":            ("coverage_code",
                            "coverage_annual_statement_line_of_business",
                            "coverage_type", "coverage"),
    "territory":           ("risk_location_country", "risk_location_subdivision",
                            "policy_risk_country", "policy_risk_subdivision",
                            "risk_state", "risk_country", "territory"),
    # Same column, opposite test — where the risk IS, checked against a list it
    # must not be on.
    "excluded_territory":  ("risk_location_country", "risk_location_subdivision",
                            "policy_risk_country", "policy_risk_subdivision",
                            "risk_state", "risk_country", "territory"),
    "permitted_risks":     ("coverage_code", "contract_class_of_business",
                            "risk_type", "occupancy", "class_of_business"),
    "excluded_risks":      ("coverage_code", "contract_class_of_business",
                            "risk_type", "occupancy", "class_of_business"),
    # No canonical column measures how long a policy runs for — the model holds
    # the two dates, not the span between them. Left as by-name candidates: a
    # bordereau that reports the term as a column is checked, and one that does
    # not is reported as unmapped rather than checked against something else.
    "policy_period_months": ("policy_term_months", "policy_period"),
    "transaction_types":   ("premium_transaction_type", "transaction_type"),
    # authority and limits
    "max_sum_insured":     ("policy_sum_insured_amount", "sum_insured",
                            "total_sum_insured"),
    "aggregate_limit":     ("coverage_aggregate_limit",
                            "coverage_participation_aggregate_limit",
                            "aggregate_exposure", "sum_insured"),
    "max_tiv":             ("coverage_total_insured_value",
                            "risk_location_total_insured_value",
                            "total_insured_value", "tiv"),
    "premium_cap_total":   ("premium_transaction_total_gross_written_premium_amount",
                            "premium_transaction_gross_premium_amount",
                            "gross_written_premium", "premium_written_total"),
    "min_premium":         ("premium_transaction_gross_premium_amount",
                            "gross_written_premium", "premium"),
    "deductible":          ("coverage_deductible_amount", "deductible", "excess"),
    "referral_threshold":  ("policy_sum_insured_amount", "sum_insured",
                            "total_sum_insured"),
    # financial
    "commission_max_pct":  ("premium_transaction_commission_percent",
                            "commission_line_rate_percent",
                            "commission_pct", "commission_rate", "commission"),
    "commission_pct":      ("premium_transaction_commission_percent",
                            "commission_line_rate_percent",
                            "commission_pct", "commission_rate", "commission"),
    "brokerage_pct":       ("premium_transaction_brokerage_percent",
                            "brokerage_pct", "brokerage_rate", "brokerage"),
    "carrier_share_pct":   ("coverage_participation_share_percent",
                            "cession_share_percent",
                            "carrier_share_pct", "share_pct", "our_share"),
    "currency":            ("policy_sum_insured_currency",
                            "premium_transaction_original_currency",
                            "currency", "premium_currency"),
    "payment_terms_days":  ("program_due_after_days", "settlement_days",
                            "payment_terms"),
    "settlement_frequency": ("program_bordereau_frequency", "bordereau_period",
                             "period"),
}

# How each limit compares. Kept apart from the sentence in AGREED_LIMITS
# ("check") because that one is written to be READ by a person on the review
# screen, and this one has to be executed.
LIMIT_OPERATORS: dict[str, str] = {
    "coverage":             "in",
    "territory":            "in",
    "excluded_territory":   "not_in",
    "permitted_risks":      "in",
    "excluded_risks":       "not_in",
    "policy_period_months": "lte",
    "transaction_types":    "in",
    "max_sum_insured":      "lte",
    "aggregate_limit":      "lte_aggregate",
    "max_tiv":              "lte",
    "premium_cap_total":    "lte_aggregate",
    "min_premium":          "gte",
    "deductible":           "gte",
    "referral_threshold":   "lte_unless_referred",
    "commission_max_pct":   "lte",
    "commission_pct":       "eq",
    "brokerage_pct":        "eq",
    "carrier_share_pct":    "eq",
    "currency":             "eq",
    "payment_terms_days":   "lte",
    "settlement_frequency": "period_complete",
}

_LIST_VALUED = {"in", "not_in"}

# Which rule-catalog template each comparison becomes, and what it calls its
# bound. The catalog (contract_upload_services/seeds/rule_templates.json) is the
# same one the extraction pipeline compiles against, so a check written FROM
# TERMS and a check read out of a PDF end up as the same kind of SQL, run by the
# same engine, reported the same way.
#
# WHY THIS EXISTS AT ALL. A rule row is not a check until something can execute
# it. duckdb_validation runs `rule_spec.compiled_sql` and nothing else — a rule
# without one is reported as "unprocessable", not silently skipped, but it is
# also not RUN. Writing the operand and stopping there left every contract-term
# check recorded, visible on the screen, counted on the record, and never once
# applied to a row.
_OPERATOR_TEMPLATE: dict[str, tuple[str, str]] = {
    "in":                 ("value_in_set", "allowed"),
    "not_in":             ("value_not_in_set", "excluded"),
    "lte":                ("max_limit", "max"),
    # A referral threshold is a cap with a way round it: the risk may be bound
    # above the figure with the carrier's prior agreement. Nothing in a
    # bordereau says whether it was referred, so the check is the cap and the
    # exception is a QUERY — which is what an exception report is for, and why
    # this limit's severity is a warning rather than a breach.
    "lte_unless_referred": ("max_limit", "max"),
    "gte":                ("min_limit", "min"),
    "lte_aggregate":      ("aggregate_cap", "max"),
}

# The kinds whose values are numbers. An "equals" on a number is a range with
# one value in it — comparing it as a member of a set would compare the TEXT,
# and "12" then fails against a cell holding 12.0.
_NUMERIC_KINDS = {"percent", "money", "int"}


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _find_column(wanted: tuple[str, ...],
                 fields: list[dict]) -> tuple[dict | None, list[dict]]:
    """The template column a limit should be checked against.

    Returns (column, ambiguous). Exactly one of the two is ever set: a column
    to check, or the columns that all claimed to be the same thing and could
    not be told apart.

    CANONICAL FIELD FIRST, and it is believed. A template mapped to the data
    model says what each of its columns MEANS, and that is a better answer than
    anything this function could infer from a heading — if it says the column
    headed "Number of Instalments" carries the policy sum insured, the place to
    correct that is the template, not here.

    THE HARD CASE IS SEVERAL COLUMNS CLAIMING ONE MEANING. Real templates have
    them: five columns on a Lloyd's bordereau are mapped to the commission
    percentage — "Commission %" and four tax rates that were auto-mapped
    alongside it. Taking whichever came last (which is what a dict of them
    does) binds the contract's commission check to "Tax 5 - %", and a rule that
    fails rows for the wrong reason is worse than no rule at all, because
    somebody acts on it. So the tie is broken by the column's own NAME, and
    only when the name settles it beyond doubt: the heading that contains the
    term this limit is looking for wins, and if none does — or two do — nothing
    is chosen and the caller reports it.
    """
    def norm_hits(key: str, w: str) -> list[dict]:
        return [f for f in fields if _norm(f.get(key) or "") == _norm(w)]

    for w in wanted:
        hits = norm_hits("canonical_field", w)
        if not hits:
            continue
        if len(hits) == 1:
            return hits[0], []
        best = _named_like(hits, wanted)
        return (best, []) if best else (None, hits)

    # Falling back to the column's own name catches a template nobody has
    # mapped yet, where "Gross Written Premium" is still a perfectly good
    # answer. First match wins: these are exact, so a second is the same column
    # twice.
    for w in wanted:
        hits = norm_hits("name", w)
        if hits:
            return hits[0], []
    return None, []


def _named_like(hits: list[dict], wanted: tuple[str, ...]) -> dict | None:
    """The one column among these whose HEADING says what the limit is after.

    Scored by the longest wanted name the heading contains, so "Location of
    risk - Country" beats "Intermediary 2 - Country" for a territory (it
    carries `riskcountry`, which the other does not) and "Commission %" beats
    "Tax 5 - %" for a commission. A tie at the top is not a winner: two
    headings equally entitled to the check means nobody can say which the
    contract meant, and guessing is the thing this module refuses to do.
    """
    scored: list[tuple[int, dict]] = []
    for f in hits:
        name = _norm(f.get("name") or "")
        best = max((len(_norm(w)) for w in wanted if _norm(w) and _norm(w) in name),
                   default=0)
        scored.append((best, f))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None
    return scored[0][1]


def _split_list(value: Any) -> list[str]:
    """"nuclear, war" → ["nuclear", "war"]. Written as prose in the contract,
    so it is split the way somebody writing prose separates things."""
    text_v = str(value)
    for sep in (";", ",", " and "):
        text_v = text_v.replace(sep, "|")
    return [p.strip() for p in text_v.split("|") if p.strip()]


def map_limits_to_template(limits: dict,
                           template_fields: list[dict]) -> tuple[list[dict], list[dict]]:
    """Agreed limits → rules, plus the ones that could not be bound.

    Returns (rules, unmapped). `unmapped` is not a failure to hide: it is the
    list of things this contract says that this template cannot be measured
    against, which the setup screen has to show before anybody relies on the
    checks being complete.
    """
    rules: list[dict] = []
    unmapped: list[dict] = []

    for key, entry in (limits or {}).items():
        spec = AGREED_LIMITS.get(key)
        if not spec or not spec.get("check"):
            continue                      # wording only, by design
        value = entry.get("value")
        if value in (None, ""):
            continue

        wanted = LIMIT_COLUMNS.get(key)
        column, ambiguous = (_find_column(wanted, template_fields) if wanted
                             else (None, []))
        if column is None:
            unmapped.append({
                "key": key, "question": spec["question"], "value": value,
                "reason": (
                    "this template has "
                    + ", ".join(f"“{f['name']}”" for f in ambiguous[:4])
                    + " all mapped to the same meaning, and nothing says which "
                      "one this term is about"
                    if ambiguous
                    else "this template has no column that measures it"),
                "looked_for": list(wanted or ()),
                # The headings that clashed, so the fix is a click away in the
                # template rather than a puzzle.
                "ambiguous": [f["name"] for f in ambiguous],
            })
            continue

        op = LIMIT_OPERATORS.get(key, "eq")
        operand = _split_list(value) if op in _LIST_VALUED else value
        severity = entry.get("severity") or spec.get("default_severity") or "warning"

        # The rule as something that can RUN, not just something that can be
        # read. See _OPERATOR_TEMPLATE for why this is the difference between a
        # check and a note.
        ir = _ir_for(op=op, kind=spec["kind"], field=column["name"],
                     operand=operand, rule_name=spec["question"])
        sql = _compile(ir, column["name"], template_fields,
                       column.get("sheet")) if ir else None

        rules.append({
            "key": key,
            "name": spec["question"],
            "description": f"{spec['question']}: {value}. Agreed in the "
                           f"contract and checked on every row.",
            "severity": severity,
            "column": column["name"],
            "sheet": column.get("sheet"),
            "canonical_field": column.get("canonical_field"),
            "operator": op,
            "operand": operand,
            "ir": ir,
            "compiled_sql": sql,
            # Said out loud so the screens can. A term whose comparison has no
            # template — "accounts are settled monthly" is not a test a row can
            # be put to — is agreed, printed and reported, and never pretends to
            # be measured.
            "executable": bool(sql),
            "error_message": _message(spec["question"], op, value, column["name"]),
        })
    return rules, unmapped


def _ir_for(*, op: str, kind: str, field: str, operand: Any,
            rule_name: str) -> dict | None:
    """The catalog rule this comparison IS, or None when there is no template
    for it.

    None is an honest answer, not a failure: `period_complete` ("accounts are
    settled monthly") is a fact about a bordereau as a whole and not a test any
    row can be put to, and inventing a per-row check for it would flag every
    row of a perfectly good file.
    """
    if op == "eq":
        if kind in _NUMERIC_KINDS:
            try:
                n = float(operand)
            except (TypeError, ValueError):
                return None
            params = {"field": field, "min": n, "max": n}
            return {"template": "range_check", "params": params,
                    "rule_name": rule_name}
        return {"template": "value_in_set",
                "params": {"field": field, "allowed": [str(operand)]},
                "rule_name": rule_name}

    mapped = _OPERATOR_TEMPLATE.get(op)
    if not mapped:
        return None
    template, bound = mapped
    if template in ("value_in_set", "value_not_in_set"):
        values = operand if isinstance(operand, list) else [operand]
        return {"template": template,
                "params": {"field": field, bound: [str(v) for v in values]},
                "rule_name": rule_name}
    try:
        n = float(operand)
    except (TypeError, ValueError):
        return None
    params: dict[str, Any] = {"field": field, bound: n}
    if template == "aggregate_cap":
        # The contract caps the TOTAL, so the check sums the column across the
        # file rather than testing each row against a figure no single row was
        # ever meant to reach.
        params["aggregation"] = "sum"
    return {"template": template, "params": params, "rule_name": rule_name}


def _compile(ir: dict, field: str, template_fields: list[dict],
             sheet: str | None) -> str | None:
    """The IR as one DuckDB SELECT returning the rows that break it.

    Deterministic — the same compiler the extraction pipeline uses, with no
    model call anywhere. Compiled against EVERY sheet that carries the column,
    not just the one the match was found on: a multi-sheet bordereau repeats the
    same headings across schedules, and a rule that checked the first sheet only
    would pass a file whose breach was on the second.

    Never raises. A term whose check cannot be compiled is still recorded as a
    rule — it is what the contract says — and the validation run reports it as
    unprocessable rather than dropping it, which is the truth about it.
    """
    from contract_upload_services.rule_compiler import compile_ir

    sheets = [f.get("sheet") for f in template_fields
              if f.get("name") == field and f.get("sheet")]
    sheets = list(dict.fromkeys(sheets)) or ([sheet] if sheet else [])
    if not sheets:
        return None
    try:
        return compile_ir(ir, {field: sheets}, default_sheet=sheets[0])
    except Exception as e:                  # never at the cost of the binding
        log.warning("[contract] %r could not be compiled to SQL: %s",
                    ir.get("rule_name"), e)
        return None


def _message(question: str, op: str, value: Any, column: str) -> str:
    """What the exception says when a row fails. Written for whoever opens the
    exception report, who knows the contract but not the rule engine."""
    if op == "not_in":
        return (f"{column} is excluded by the contract "
                f"({question.lower()}: {value}).")
    if op in ("in",):
        return f"{column} is outside what the contract allows ({value})."
    if op in ("lte", "lte_aggregate", "lte_unless_referred"):
        return f"{column} is above the agreed {question.lower()} of {value}."
    if op == "gte":
        return f"{column} is below the agreed {question.lower()} of {value}."
    return f"{column} does not match the agreed {question.lower()} of {value}."


def write_rules(conn, *, contract_id: int, program_id: int | None,
                tenant_id: int | None, rules: list[dict],
                template_id: int | None) -> int:
    """Replace this contract's rules with the ones given. Returns how many.

    REPLACE, not append: mapping the same contract to the same template twice
    must leave one set of checks, not two that both fire on every row. The
    contract's clauses are untouched — they are what it SAYS, and this only
    rewrites what is CHECKED.
    """
    # 'custom' and 'input', because the table constrains both: the engine to
    # ajv | custom | global, and the stage to input | output | both. A
    # comparison against a bordereau column is a custom check run on the way
    # IN — which is also where every rule already in this table sits.
    conn.execute(text("DELETE FROM rule_sql WHERE contract_id = :cid"),
                 {"cid": contract_id})
    conn.execute(text("DELETE FROM validation_rule WHERE contract_id = :cid"),
                 {"cid": contract_id})

    for r in rules:
        conn.execute(
            text("""
                INSERT INTO validation_rule
                    (tenant_id, contract_id, program_id, rule_engine,
                     rule_name, rule_description, validation_stage, severity,
                     canonical_target, rule_spec, error_message,
                     generation_confidence, rule_status, created_by)
                VALUES
                    (:tenant_id, :contract_id, :program_id, 'custom',
                     :rule_name, :rule_description, 'input', :severity,
                     CAST(:canonical_target AS JSONB), CAST(:rule_spec AS JSONB),
                     :error_message, :confidence, 'active', 'contract_terms')
            """),
            {
                "tenant_id": tenant_id,
                "contract_id": contract_id,
                "program_id": program_id,
                "rule_name": r["name"],
                "rule_description": r["description"],
                "severity": r["severity"],
                "canonical_target": json.dumps({
                    "output_field": r["column"],
                    "sheet": r.get("sheet"),
                    "canonical_field": r.get("canonical_field"),
                }),
                "rule_spec": json.dumps({
                    "source": "contract_terms",
                    "limit": r["key"],
                    "operator": r["operator"],
                    "operand": r["operand"],
                    "output_template_id": template_id,
                    # What the validation engine actually runs. Everything above
                    # is provenance — this is the check. See _compile; absent
                    # means the run reports the clause as unprocessable, which
                    # is honest and visible, rather than passing a file nothing
                    # was measured against.
                    #
                    # The IR is stored WHOLE, under the same key every other
                    # rule in this table uses, because the compiled query is a
                    # cache and the IR is the rule. The engine recompiles from
                    # it when the file in front of it does not have the sheets
                    # the query names (a template on another jurisdiction's
                    # layout, a new version, differently named tabs) — with
                    # only the flattened template/params it cannot, and the
                    # term goes quiet for good. See
                    # duckdb_validation._refresh_if_stale.
                    "ir": r.get("ir"),
                    "template": (r.get("ir") or {}).get("template"),
                    "params": (r.get("ir") or {}).get("params"),
                    "compiled_sql": r.get("compiled_sql"),
                }),
                "error_message": r["error_message"],
                # Not a guess. The carrier typed this number into a row that
                # already knew what it meant, so there is nothing to be
                # uncertain about — unlike a rule read out of a PDF.
                "confidence": 1.0,
            })
    return len(rules)
