"""
contract_types.py
─────────────────
WHAT a contract is, and therefore which fields it cannot be saved without.

There are two, and they differ by who the carrier is facing:

  insurer_broker      the insurer delegates to a BROKER. A binder / delegated
                      authority: the broker writes business in the insurer's
                      name, under a stated class of business.

  insurer_reinsurer   the insurer cedes to a REINSURER. A treaty: nobody writes
                      on anyone's behalf. What identifies it is the year of
                      account it attaches to and the notice needed to get out
                      of it.

Both lists live HERE and nowhere else. The create/edit endpoints validate
against this module, and the frontend form is BUILT from it (served by
GET /contract-types), so a field cannot be mandatory on the server and optional
in the form — the two cannot drift because there is only one list.

Changing what a type requires is a one-line edit to `required` below. Nothing
else in the app hardcodes a field list.
"""
from __future__ import annotations

from typing import Any

# ── the field vocabulary ────────────────────────────────────────────────────
# `attr` is the Contract ORM attribute the value lands on, so the routes can
# apply an edit generically instead of naming all eighteen columns twice.
# `kind` is what the form should render, not a storage type.
FIELDS: dict[str, dict[str, Any]] = {
    "name": {
        "attr": "name", "label": "Contract name", "kind": "text",
        "hint": "How people will refer to it — e.g. “Schedule A — 2027”.",
    },
    "counterparty_party_id": {
        "attr": "broker_party_id", "label": "Counterparty", "kind": "party",
        "hint": "The organisation on the other side of this contract.",
    },
    "schedule_key": {
        "attr": "schedule_key", "label": "Schedule key", "kind": "text",
        # The design's own hint, verbatim.
        "hint": "Lets one programme hold many contracts.",
    },
    "inception_dt": {
        "attr": "inception_dt", "label": "Inception", "kind": "date",
        "hint": "The day cover under this contract begins.",
    },
    "expiry_dt": {
        "attr": "expiry_dt", "label": "Expiry", "kind": "date",
        "hint": "The day it ends. A renewal starts a NEW contract rather than "
                "extending this one, so the history stays readable.",
    },
    "class_of_business": {
        "attr": "class_of_business", "label": "Class of business", "kind": "text",
        "hint": "What may be written under it, e.g. “Commercial Auto”.",
    },
    "risk_code": {
        "attr": "risk_code", "label": "Risk code", "kind": "text",
        "hint": "Lloyd's risk code, where one applies.",
    },
    "section_number": {
        "attr": "section_number", "label": "Section", "kind": "text",
        "hint": "For a contract written in sections, which one this row is.",
    },
    "year_of_account": {
        "attr": "year_of_account", "label": "Year of account", "kind": "text",
        "hint": "The account year the cession attaches to — not the same as "
                "the inception year for a treaty written mid-year.",
    },
    "notice_period_days": {
        "attr": "notice_period_days", "label": "Notice period (days)", "kind": "int",
        "hint": "Days of notice needed to cancel. Termination is checked "
                "against this.",
    },
    "premium_cap_amount": {
        "attr": "premium_cap_amount", "label": "Premium cap", "kind": "decimal",
        # Says so on the field, because the two are easy to set by accident and
        # only one of them is enforced.
        "hint": "Recorded on the contract only. To have it CHECKED on every "
                "file, set “Most premium they may write” in the limits below "
                "instead — that one produces a clause and a check.",
    },
    "premium_cap_currency": {
        "attr": "premium_cap_currency", "label": "Cap currency", "kind": "currency",
        "hint": "Currency the cap is expressed in.",
    },
    "executed_date": {
        "attr": "executed_date", "label": "Executed", "kind": "date",
        "hint": "When it was signed. Normally filled in when the broker signs "
                "and returns it — set it here only for a contract that was "
                "already executed before it reached Kavachio.",
    },
    "earnings_pattern": {
        "attr": "earnings_pattern", "label": "Earnings pattern", "kind": "text",
        "hint": "How premium earns across the term, where it is stated.",
    },
}

# ── the two types ───────────────────────────────────────────────────────────
# `counterparty_party_type` is enforced, not decorative: an insurer_broker
# contract filed against a reinsurer would sit under a party whose page cannot
# show it, and every downstream screen joins through that column.
CONTRACT_TYPES: dict[str, dict[str, Any]] = {
    "insurer_broker": {
        "key": "insurer_broker",
        "label": "Insurer ↔ Broker",
        "blurb": "A binder / delegated authority: the broker writes business "
                 "in your name under agreed terms.",
        "counterparty_party_type": "broker",
        "counterparty_label": "Broker",
        # A broker must be ON the programme before it can hold a contract there
        # — program_broker is the gate. A reinsurer has no such gate, because it
        # does not produce business into the programme.
        "counterparty_must_be_on_programme": True,
        "required": [
            "name", "counterparty_party_id", "inception_dt", "expiry_dt",
            "class_of_business",
        ],
        "optional": [
            "schedule_key", "risk_code", "section_number", "year_of_account",
            "notice_period_days", "premium_cap_amount", "premium_cap_currency",
            "executed_date", "earnings_pattern",
        ],
    },
    "insurer_reinsurer": {
        "key": "insurer_reinsurer",
        "label": "Insurer ↔ Reinsurer",
        "blurb": "A treaty: you cede risk to a reinsurer. Nobody writes on "
                 "anyone's behalf, so nothing is delegated.",
        "counterparty_party_type": "reinsurer",
        "counterparty_label": "Reinsurer",
        "counterparty_must_be_on_programme": False,
        "required": [
            "name", "counterparty_party_id", "inception_dt", "expiry_dt",
            "year_of_account", "notice_period_days",
        ],
        "optional": [
            "schedule_key", "class_of_business", "risk_code", "section_number",
            "premium_cap_amount", "premium_cap_currency", "executed_date",
            "earnings_pattern",
        ],
    },
}

# The optional lists are long, and that is fine: the form shows the required
# fields and folds the rest behind a disclosure, so a contract that needs none
# of them is six inputs and one closed section. Two carry a caveat the form
# states on the field itself rather than here:
#
#   · PREMIUM CAP also exists as the agreed limit "Most premium they may write",
#     which unlike this field produces a clause and a check. Setting both is two
#     places to state one number.
#   · EXECUTED DATE is normally written by signing (submit_signed), not typed —
#     it is here for a contract already executed before it reached Kavachio.
#
# UMR is NOT in either list. The column stays and the record still shows it —
# extraction reads one off an uploaded wording — but it is not asked for when a
# contract is raised, and cannot be required. A UMR is issued by the market, not
# invented in a form: making it mandatory at creation forced people to type a
# placeholder, which is worse than an empty field because a placeholder looks
# like an answer. It gets its own flow.
DEFAULT_TYPE = "insurer_broker"


# ── lifecycle ───────────────────────────────────────────────────────────────
# The BUSINESS state of a contract, kept apart from `approval_status` (the
# carrier's decision) and `status_ops` (what the extraction pipeline did with
# the file). Three axes, because they answer three different questions and a
# single column that tried to carry all three would lose two of them.
#
#   draft              being prepared. Not visible to the counterparty.
#   pending            the BROKER submitted it; waiting on the carrier.
#   in_review          the CARRIER sent its terms out; waiting on the broker.
#   changes_requested  the broker pushed back; the ball is with the carrier.
#   agreed             both sides settled the terms. Next stop is signature.
#   signed             the broker signed and returned it. It is back with the
#                      carrier, whose next act is PLACEMENT — which Kavachio
#                      does not do yet. Until it does, the carrier goes straight
#                      from here to putting the contract in force, and this
#                      state is where a placement step would slot in without
#                      moving anything either side of it.
#   active             in force. The only state a bordereau can be produced
#                      against.
#   expired            term ran out. Reached by the date, not by a decision.
#   terminated         ended early and deliberately, with a reason on the record.
#   superseded         replaced by a renewal, which points back at it.
#
# Two directions of travel meet here, which is why there are two "waiting"
# states rather than one:
#
#   BROKER-ORIGINATED   draft → pending → (carrier approves) → active
#                       The broker brings a contract; the carrier decides.
#
#   CARRIER-ORIGINATED  draft → in_review ⇄ changes_requested
#                             → agreed → signed → active
#                       The carrier proposes terms; the broker reviews, and may
#                       push back as many times as it takes. Nobody "approves"
#                       here — the carrier already owns the book, so what is
#                       being sought is the broker's AGREEMENT, which is a
#                       different thing and needs its own word. The broker then
#                       signs and returns it, and the carrier places it and puts
#                       it in force.
#
# A carrier can still raise a contract straight into `active` when there is
# nothing to negotiate, which is what it did before any of this existed.
LIFECYCLE = (
    "draft", "pending", "in_review", "changes_requested", "agreed", "signed",
    "active", "expired", "terminated", "superseded",
)

# Which moves are legal. A contract cannot go back to draft once submitted to
# the carrier — a rejected contract is corrected and re-submitted, which is an
# edit plus a fresh decision, not a rewind.
LIFECYCLE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    # `signed` from a draft is how a contract with no broker seat gets there:
    # an insurer ↔ reinsurer counterparty never logs in, so its signature is
    # recorded rather than typed, and there is no review round in between.
    # `active` is NOT reachable from here any more — a contract goes in force
    # because both sides signed it, which means passing through `signed`.
    "draft":             ("pending", "in_review", "signed", "terminated"),
    "pending":           ("signed", "draft", "terminated"),
    # The negotiation loop. in_review ⇄ changes_requested can run as many times
    # as the two sides need; neither side can end it alone.
    "in_review":         ("changes_requested", "agreed", "draft", "terminated"),
    "changes_requested": ("in_review", "draft", "terminated"),
    # Agreed is not final: either side can reopen before signature, and pretending
    # otherwise would mean re-raising the whole contract to change one number.
    # It leads to `signed`, never straight to `active` — signing is the step.
    "agreed":            ("signed", "changes_requested", "terminated"),
    # Signed by both sides. `active` is the only way on for now because
    # placement is not built — when it is, it goes here. Withdrawing a
    # signature before the contract is in force sends it back the way it came,
    # which is done by setting the state directly rather than as a transition:
    # it is an erasure, not a step forward.
    "signed":            ("active", "changes_requested", "terminated"),
    "active":            ("expired", "terminated", "superseded"),
    "expired":           ("superseded",),
    "terminated":        (),
    "superseded":        (),
}

# The states in which the contract is out with the broker rather than with the
# carrier. Used to decide who may edit and whose queue it belongs in.
WITH_BROKER = ("in_review", "agreed")
WITH_CARRIER = ("draft", "changes_requested", "signed")


class ContractTypeError(ValueError):
    """A contract that cannot be saved as the type it claims to be.

    Carries `errors` as {field: message} so the form can mark the offending
    inputs rather than showing one sentence above the whole thing.
    """

    def __init__(self, message: str, errors: dict[str, str] | None = None):
        super().__init__(message)
        self.message = message
        self.errors = errors or {}

    def to_detail(self) -> dict[str, Any]:
        return {"message": self.message, "errors": self.errors}


def spec(contract_type: str) -> dict[str, Any]:
    """The type's definition, or a 400-worthy error naming the valid ones."""
    t = CONTRACT_TYPES.get((contract_type or "").strip())
    if t is None:
        raise ContractTypeError(
            f"“{contract_type}” is not a contract type. "
            f"Use one of: {', '.join(CONTRACT_TYPES)}.",
            {"contract_type": "unknown contract type"},
        )
    return t


def field_names(contract_type: str) -> list[str]:
    """Every field this type accepts, required first — the form's field order."""
    t = spec(contract_type)
    return [*t["required"], *t["optional"]]


def public_spec() -> list[dict[str, Any]]:
    """The whole vocabulary, shaped for the form that renders it.

    Sent to the browser so the create/edit screen knows which inputs to show
    and which to mark mandatory WITHOUT restating any of it in TypeScript.
    """
    out = []
    for t in CONTRACT_TYPES.values():
        fields = []
        for name in field_names(t["key"]):
            f = FIELDS[name]
            fields.append({
                "name": name,
                "label": (t["counterparty_label"]
                          if name == "counterparty_party_id" else f["label"]),
                "kind": f["kind"],
                "hint": f["hint"],
                "required": name in t["required"],
            })
        out.append({
            "key": t["key"],
            "label": t["label"],
            "blurb": t["blurb"],
            "counterparty_party_type": t["counterparty_party_type"],
            "counterparty_label": t["counterparty_label"],
            "counterparty_must_be_on_programme": t["counterparty_must_be_on_programme"],
            "fields": fields,
        })
    return out


def _is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def validate(contract_type: str, values: dict[str, Any],
             *, partial: bool = False) -> dict[str, Any]:
    """Check `values` against the type and return only the fields it allows.

    `partial=True` (an edit) skips the mandatory check for fields the caller did
    not send, so a screen can save one field without resending the whole record
    — but a field that IS sent may not be blanked if the type requires it.

    Unknown fields are DROPPED rather than rejected: a field that is optional on
    one type and absent on the other would otherwise make switching type fail
    with an error about a field the user cannot see.
    """
    t = spec(contract_type)
    allowed = set(field_names(t["key"]))
    clean = {k: v for k, v in values.items() if k in allowed}

    errors: dict[str, str] = {}
    for name in t["required"]:
        if partial and name not in values:
            continue
        if _is_blank(clean.get(name)):
            errors[name] = f"{FIELDS[name]['label']} is required for a "\
                           f"{t['label']} contract."

    # A term that runs backwards is not a term. Checked here rather than in the
    # route so the manual-entry and the extracted-metadata paths agree.
    inc, exp = clean.get("inception_dt"), clean.get("expiry_dt")
    if inc and exp and str(inc) > str(exp):
        errors["expiry_dt"] = "Expiry falls before inception."

    days = clean.get("notice_period_days")
    if days not in (None, "") and int(days) < 0:
        errors["notice_period_days"] = "A notice period cannot be negative."

    if errors:
        # Name them. "Missing something" is true and useless — the caller may
        # be three steps away from the field, and a message that does not say
        # which one leaves them hunting.
        named = ", ".join(FIELDS[k]["label"] for k in errors if k in FIELDS)
        raise ContractTypeError(
            f"A {t['label']} contract cannot be saved without: {named}."
            if named else
            f"This {t['label']} contract is missing something it cannot be "
            f"saved without.", errors)
    return clean


# ── the limits you agreed ───────────────────────────────────────────────────
# The heart of writing a contract. Each entry is ONE row of the design's step-1
# table, and it carries three things at once:
#
#     the question   in the words somebody says out loud ("Most commission you
#                    will pay"), not the column name (commission_pct)
#     the answer     what was agreed
#     the teeth      what happens when a file breaks it — "Stop the row" or
#                    "Just flag it"
#
# That third column is the point of the whole flow. Uploading a PDF gets you
# clauses a model had to interpret and a severity somebody guessed at later;
# typing the number here means the carrier decides enforcement AT THE MOMENT OF
# AGREEING IT, in plain words. It maps straight onto validation_rule.severity,
# which the pipeline already understands.
#
# `token` is what the wording quotes. Sections store {{key}}, never the baked
# value — see contract_wording. Change the cap in step 1 and every sentence
# carrying its chip changes with it, and so does the check. A hand-typed "15%"
# does not, which is exactly how a contract ends up saying one thing while the
# system checks another.
#
# `check` is the expression the limit becomes. None means the term is real and
# worth recording but has nothing in a spreadsheet to measure it against —
# governing law, tax treatment — so it stays wording and produces no check.
AGREED_LIMITS: dict[str, dict[str, Any]] = {
    # ══ COVER — what may be written at all ══
    "coverage": {
        "question": "What is covered",
        "sub": "Coverage — e.g. Property Damage, Business Interruption.",
        "kind": "text", "default_severity": "warning",
        "check": "coverage in ({v})", "token": "{v}", "group": "cover",
    },
    "territory": {
        "question": "Where business may be written",
        "sub": "Territory — the places the broker is allowed to sell in.",
        "kind": "text", "default_severity": "warning",
        "check": "risk_territory in ({v})", "token": "{v}", "group": "cover",
    },
    "permitted_risks": {
        "question": "What may be written",
        "sub": "Permitted risks — e.g. commercial buildings. Anything else "
               "needs referring.",
        "kind": "text", "default_severity": "critical",
        "check": "risk_type in ({v})", "token": "{v}", "group": "cover",
    },
    "excluded_risks": {
        "question": "What may never be written",
        "sub": "Excluded risks — e.g. nuclear, war. No referral makes these "
               "acceptable.",
        "kind": "text", "default_severity": "critical",
        "check": "risk_type not in ({v})", "token": "{v}", "group": "cover",
    },
    "policy_period_months": {
        "question": "Longest policy you will write",
        "sub": "Policy period, in months. A longer one is outside this "
               "contract.",
        "kind": "int", "unit": "months", "default_severity": "warning",
        "check": "policy_term_months <= {v}", "token": "{v} months",
        "group": "cover",
    },
    "transaction_types": {
        "question": "Kinds of transaction you will accept",
        "sub": "Anything else in a file is not covered by this contract.",
        "kind": "choice", "default_severity": "critical",
        "choices": ["new, renewal, endorsement and cancellation",
                    "new and renewal only"],
        "check": "transaction_type in ({v})", "token": "{v}", "group": "cover",
    },

    # ══ AUTHORITY / LIMITS — how much they may commit you to ══
    "max_sum_insured": {
        "question": "Biggest risk you will cover",
        "sub": "Per risk limit — sum insured on any one policy.",
        "kind": "money", "default_severity": "critical",
        "check": "sum_insured <= {v}", "token": "{c} {v}", "group": "authority",
    },
    "aggregate_limit": {
        "question": "Most you will be on risk for at once",
        "sub": "Aggregate limit — total exposure across everything written "
               "under this contract.",
        "kind": "money", "default_severity": "critical",
        "check": "aggregate_exposure <= {v}", "token": "{c} {v}",
        "group": "authority",
    },
    "max_tiv": {
        "question": "Most total insured value on one policy",
        "sub": "Maximum TIV — the sum of all values on a single risk, which "
               "can exceed the limit you pay out.",
        "kind": "money", "default_severity": "critical",
        "check": "total_insured_value <= {v}", "token": "{c} {v}",
        "group": "authority",
    },
    "premium_cap_total": {
        "question": "Most premium they may write",
        "sub": "Maximum premium — across the whole term on this contract. At "
               "85% you get an email, before the line rather than after.",
        "kind": "money", "default_severity": "critical",
        "check": "premium_written_total <= {v}", "token": "{c} {v}",
        "group": "authority",
    },
    "min_premium": {
        "question": "Smallest premium you will take",
        "sub": "Minimum premium, per policy. Anything cheaper is not worth "
               "writing.",
        "kind": "money", "default_severity": "warning",
        "check": "gross_written_premium >= {v}", "token": "{c} {v}",
        "group": "authority",
    },
    "deductible": {
        "question": "Least the insured must bear",
        "sub": "Deductible — the minimum each claim carries before you pay.",
        "kind": "money", "default_severity": "warning",
        "check": "deductible >= {v}", "token": "{c} {v}", "group": "authority",
    },
    "referral_threshold": {
        "question": "Above which they must ask you first",
        "sub": "Referral threshold — a risk over this may not be bound on "
               "their own authority.",
        "kind": "money", "default_severity": "warning",
        "check": "sum_insured <= {v} unless referred", "token": "{c} {v}",
        "group": "authority",
    },
    "underwriting_authority": {
        "question": "How much they may decide themselves",
        "sub": "Underwriting authority — delegated means they bind within "
               "these limits without asking.",
        "kind": "choice", "default_severity": None,
        "choices": ["delegated within these limits",
                    "referral on every risk",
                    "delegated, with referral above the threshold"],
        "check": None, "token": "{v}", "group": "authority",
    },

    # ══ FINANCIAL TERMS — what the two of you pay each other ══
    "commission_max_pct": {
        "question": "Most commission you will pay",
        "sub": "Out of the premium on each risk.",
        "kind": "percent", "unit": "%", "default_severity": "critical",
        "check": "commission_pct <= {v}", "token": "{v}%", "group": "financial",
    },
    "commission_pct": {
        "question": "Commission",
        "sub": "The agreed rate, where it is fixed rather than capped.",
        "kind": "percent", "unit": "%", "default_severity": "critical",
        "check": "commission_pct == {v}", "token": "{v}%", "group": "financial",
    },
    "brokerage_pct": {
        "question": "Brokerage",
        "sub": "On placement, where it is separate from commission.",
        "kind": "percent", "unit": "%", "default_severity": "warning",
        "check": "brokerage_pct == {v}", "token": "{v}%", "group": "financial",
    },
    "carrier_share_pct": {
        "question": "Your share of each risk",
        "sub": "Where the contract is not 100% yours.",
        "kind": "percent", "unit": "%", "default_severity": "critical",
        "check": "carrier_share_pct == {v}", "token": "{v}%",
        "group": "financial",
    },
    "override_commission_pct": {
        "question": "Override commission",
        "sub": "Additional commission over the base rate.",
        "kind": "percent", "unit": "%", "default_severity": "warning",
        "check": None, "token": "{v}%", "group": "financial",
    },
    "profit_commission_pct": {
        "question": "Profit commission",
        "sub": "The broker's share of underwriting profit, settled at the end "
               "of the term.",
        "kind": "percent", "unit": "%", "default_severity": None,
        "check": None, "token": "{v}%", "group": "financial",
    },
    "broker_fee": {
        "question": "Broker fee",
        "sub": "A flat fee, where one is charged as well as or instead of "
               "brokerage.",
        "kind": "money", "default_severity": None,
        "check": None, "token": "{c} {v}", "group": "financial",
    },
    "premium_basis": {
        "question": "What the percentages are calculated on",
        "sub": "Premium basis — gross is before deductions, net is after.",
        "kind": "choice", "default_severity": None,
        "choices": ["gross written premium", "net written premium"],
        "check": None, "token": "{v}", "group": "financial",
    },
    "currency": {
        "question": "Contract currency",
        "sub": "Every monetary amount reported under this contract.",
        "kind": "text", "default_severity": "critical",
        "check": "currency == {v}", "token": "{v}", "group": "financial",
    },
    "payment_terms_days": {
        "question": "Days to settle",
        "sub": "Payment terms — after the end of the period the premium was "
               "written in.",
        "kind": "int", "unit": "days", "default_severity": "warning",
        "check": "settled_within_days <= {v}", "token": "{v} days",
        "group": "financial",
    },
    "settlement_frequency": {
        "question": "How often accounts are settled",
        "sub": "Settlement frequency — usually the same rhythm as the "
               "bordereau.",
        "kind": "choice", "default_severity": "warning",
        "choices": ["monthly", "quarterly"],
        "check": "bordereau period complete", "token": "{v}",
        "group": "financial",
    },
    "tax_treatment": {
        "question": "Tax treatment",
        "sub": "Taxes — whether reported premium includes taxes and levies.",
        "kind": "choice", "default_severity": None,
        "choices": ["exclusive of taxes", "inclusive of taxes",
                    "with taxes applicable and shown separately"],
        "check": None, "token": "{v}", "group": "financial",
    },
}

# The three headings the table is grouped under, in the order they are asked.
# Cover first because it decides whether a risk belongs here at all; then how
# much authority the broker has over it; then what the two of you are paid.
LIMIT_GROUPS = [
    ("cover", "Cover",
     "what may be written under this contract at all"),
    ("authority", "Authority and limits",
     "how much the broker may commit you to without asking"),
    ("financial", "Financial terms",
     "what the two of you are paying each other"),
]

SEVERITIES = ("critical", "warning")


def clean_agreed_limits(raw: dict | None) -> dict:
    """Keep known limits, coerce their values, and settle each one's severity.

    Shape in and out is {key: {"value": ..., "severity": "critical"|"warning"}}.
    A limit with no value is dropped entirely — a row with teeth but no number
    would generate a check against nothing. Severity falls back to the limit's
    own default, and is forced to None where the limit produces no check, so a
    term that cannot be checked can never claim to stop a row.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, spec in AGREED_LIMITS.items():
        entry = raw.get(key)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            entry = {"value": entry}
        v = entry.get("value")
        if v in (None, ""):
            continue
        kind = spec["kind"]
        if kind in ("percent", "money"):
            try:
                v = float(str(v).replace(",", "").strip())
            except (TypeError, ValueError):
                continue
        elif kind == "int":
            try:
                v = int(str(v).replace(",", "").strip())
            except (TypeError, ValueError):
                continue
        else:
            v = str(v).strip()

        sev = entry.get("severity") or spec.get("default_severity")
        if not spec.get("check"):
            sev = None
        elif sev not in SEVERITIES:
            sev = spec.get("default_severity")
        out[key] = {"value": v, "severity": sev}
    return out


def limit_groups_spec() -> list[dict[str, str]]:
    """The table's headings, served so the form does not restate them."""
    return [{"key": k, "label": lbl, "sub": sub} for k, lbl, sub in LIMIT_GROUPS]


def agreed_limits_spec() -> list[dict[str, Any]]:
    """The vocabulary, shaped for the step-1 table."""
    return [{
        "name": k,
        "question": s["question"],
        "sub": s["sub"],
        "kind": s["kind"],
        "unit": s.get("unit"),
        "choices": s.get("choices"),
        "group": s["group"],
        # Whether this row gets the "if a file breaks it" control at all.
        "checkable": bool(s.get("check")),
        "default_severity": s.get("default_severity"),
    } for k, s in AGREED_LIMITS.items()]
