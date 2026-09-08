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
from typing import Any

from sqlalchemy import text

from contract_types import AGREED_LIMITS

# Which canonical column each limit is checked against. The names on the right
# are the DATA MODEL's, matched against a template column's `canonical_field`
# first and its literal name second — a template mapped to the data model gives
# an exact hit, and one that was never mapped can still match by column name.
#
# A limit absent from here has no column to check and is recorded as wording
# only; see AGREED_LIMITS, where `check` is None for exactly those.
LIMIT_COLUMNS: dict[str, tuple[str, ...]] = {
    # cover
    "coverage":            ("coverage_type", "coverage"),
    "territory":           ("risk_state", "risk_country", "territory"),
    "permitted_risks":     ("risk_type", "occupancy", "class_of_business"),
    "excluded_risks":      ("risk_type", "occupancy", "class_of_business"),
    "policy_period_months": ("policy_term_months", "policy_period"),
    "transaction_types":   ("transaction_type",),
    # authority and limits
    "max_sum_insured":     ("sum_insured", "total_sum_insured"),
    "aggregate_limit":     ("aggregate_exposure", "sum_insured"),
    "max_tiv":             ("total_insured_value", "tiv"),
    "premium_cap_total":   ("gross_written_premium", "premium_written_total"),
    "min_premium":         ("gross_written_premium", "premium"),
    "deductible":          ("deductible", "excess"),
    "referral_threshold":  ("sum_insured", "total_sum_insured"),
    # financial
    "commission_max_pct":  ("commission_pct", "commission_rate", "commission"),
    "commission_pct":      ("commission_pct", "commission_rate", "commission"),
    "brokerage_pct":       ("brokerage_pct", "brokerage_rate", "brokerage"),
    "carrier_share_pct":   ("carrier_share_pct", "share_pct", "our_share"),
    "currency":            ("currency", "premium_currency"),
    "payment_terms_days":  ("settlement_days", "payment_terms"),
    "settlement_frequency": ("bordereau_period", "period"),
}

# How each limit compares. Kept apart from the sentence in AGREED_LIMITS
# ("check") because that one is written to be READ by a person on the review
# screen, and this one has to be executed.
LIMIT_OPERATORS: dict[str, str] = {
    "coverage":             "in",
    "territory":            "in",
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


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _find_column(wanted: tuple[str, ...], fields: list[dict]) -> dict | None:
    """The template column a limit should be checked against, or None.

    Canonical field first — a template mapped to the data model says exactly
    what each column means. Falling back to the column's own name catches a
    template nobody has mapped yet, where "Gross Written Premium" is still a
    perfectly good answer.
    """
    by_canonical = {_norm(f.get("canonical_field") or ""): f
                    for f in fields if f.get("canonical_field")}
    for w in wanted:
        hit = by_canonical.get(_norm(w))
        if hit:
            return hit
    by_name = {_norm(f.get("name") or ""): f for f in fields}
    for w in wanted:
        hit = by_name.get(_norm(w))
        if hit:
            return hit
    return None


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
        column = _find_column(wanted, template_fields) if wanted else None
        if column is None:
            unmapped.append({
                "key": key, "question": spec["question"], "value": value,
                "reason": "this template has no column that measures it",
                "looked_for": list(wanted or ()),
            })
            continue

        op = LIMIT_OPERATORS.get(key, "eq")
        operand = _split_list(value) if op in _LIST_VALUED else value
        severity = entry.get("severity") or spec.get("default_severity") or "warning"

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
            "error_message": _message(spec["question"], op, value, column["name"]),
        })
    return rules, unmapped


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
                }),
                "error_message": r["error_message"],
                # Not a guess. The carrier typed this number into a row that
                # already knew what it meant, so there is nothing to be
                # uncertain about — unlike a rule read out of a PDF.
                "confidence": 1.0,
            })
    return len(rules)
