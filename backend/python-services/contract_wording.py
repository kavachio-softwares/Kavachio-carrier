"""
contract_wording.py
───────────────────
The wording, WRITTEN by Kavachio from the terms the carrier typed.

Steps 2 and 3 of writing a contract. The carrier answers step 1 once — the
term, the parties, the limits — and this turns those answers into sections,
into the checks they imply, and — when somebody asks for it — into a PDF.

THE WORDING IS NOT A DOCUMENT. It is the contract's own text, held as sections
and read on the screen, where it is live: change a term and every sentence
quoting it changes with it. A file is only ever produced on demand, for the one
thing a screen cannot do — be sent, printed or signed — and it is a DIFFERENT
artefact from what is on screen: fixed, paginated, and out of date the moment a
term moves. Nothing is stored, because a stored copy is a second version of the
contract that stops following its terms.

THE ONE IDEA THIS FILE EXISTS FOR: a section body never contains a value. It
contains a TOKEN — `{{commission_max_pct}}` — resolved only when the wording is
rendered. The design puts it plainly: change the cap and every sentence
carrying its chip changes with it, and so does the check behind it; type "15%"
by hand and the two quietly drift apart, which is how a contract ends up saying
one thing while the system checks another. Storing tokens is what makes the
first behaviour the default and the second impossible by accident.

Everything here is deterministic — no model call anywhere. Same terms, same
document, every time. That is worth more than it sounds: a contract WRITTEN
from its own terms never has a clause the checks cannot read, because the
clause and the check are generated from the same number.

  build_sections()   the terms → tokenised sections
  render()           tokenised body → the words a person reads
  derive_checks()    the terms → the checks, each carrying the severity the
                     carrier chose in step 1
  compose_pdf()      the sections → a PDF, signature page included, composed
                     on demand and stored nowhere. A PDF because this copy is
                     for reading and signing, not editing — the editable
                     version is the screen, and it is the live one.
"""
from __future__ import annotations

import io
import re
from typing import Any

import esign_pdf
from contract_types import AGREED_LIMITS, LIMIT_GROUPS

# Rough plain-text capacity of one A4 page of contract prose. Only the PREVIEW
# uses this (the page tiles, "4 pages"); the real count is whatever the .docx
# renders to. An estimate is fine for a preview and is labelled as one.
_CHARS_PER_PAGE = 1800

_TOKEN = re.compile(r"\{\{([a-z_]+)\}\}")


def _money(v: Any, currency: str | None) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    body = f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"
    return f"{currency} {body}".strip() if currency else body


def _pct(v: Any) -> str:
    try:
        return f"{float(v):g}%"
    except (TypeError, ValueError):
        return str(v)


def token_values(*, values: dict, limits: dict,
                 carrier_name: str | None = None,
                 counterparty_name: str | None = None,
                 programme_name: str | None = None) -> dict[str, str]:
    """Every token a section may quote, resolved to display text.

    One place, so the wording, the preview and the .docx can never disagree
    about what a term says.
    """
    v, lim = values or {}, limits or {}
    currency = (lim.get("currency") or {}).get("value")

    out: dict[str, str] = {}
    for key, entry in lim.items():
        spec = AGREED_LIMITS.get(key)
        if not spec:
            continue
        raw = entry.get("value")
        kind = spec["kind"]
        if kind == "percent":
            out[key] = _pct(raw)
        elif kind == "money":
            out[key] = _money(raw, currency)
        elif kind == "int":
            unit = spec.get("unit")
            out[key] = f"{raw} {unit}".strip() if unit else str(raw)
        else:
            out[key] = str(raw)

    out.update({
        "contract_name": v.get("name") or "this contract",
        "inception": str(v.get("inception_dt") or ""),
        "expiry": str(v.get("expiry_dt") or ""),
        "class_of_business": v.get("class_of_business") or "",
        "carrier_name": carrier_name or "the Carrier",
        "counterparty_name": counterparty_name or "the Counterparty",
        "programme_name": programme_name or "",
        "notice_period_days": (f"{v['notice_period_days']} days"
                               if v.get("notice_period_days") else ""),
    })
    return {k: val for k, val in out.items() if str(val).strip()}


def render(body: str, tokens: dict[str, str]) -> str:
    """Resolve `{{token}}`s for reading. An unresolved token is left visible
    rather than blanked — a sentence with a hole in it is a bug somebody can
    see, and a sentence that silently lost its number is not."""
    return _TOKEN.sub(lambda m: tokens.get(m.group(1), m.group(0)), body or "")


def used_tokens(body: str) -> list[str]:
    return list(dict.fromkeys(_TOKEN.findall(body or "")))


def build_sections(*, values: dict, limits: dict,
                   type_label: str | None = None) -> list[dict]:
    """The sections Kavachio writes, in reading order.

    A section is produced only when it has something to say — one whose only
    input is missing is left out entirely, because "Commission: __%" is worse
    than no commission section. `locked` marks the one section every contract
    must carry: who the parties are.

    HOW LONG A CLAUSE IS. Every sentence here says enough to be understood by
    somebody who has not read the terms table — "It covers C." is a true
    statement that answers nothing, and a reader who has to hold the schedule
    beside the wording to work out what a clause means will not read the
    wording. So each states what it applies to and what follows from it. The
    ceiling is one line on the page: past that a clause stops being read, and
    anything that needs a second line is a second clause.
    """
    v, lim = values or {}, limits or {}
    has = lambda *keys: any(k in lim for k in keys)  # noqa: E731
    out: list[dict] = []

    def add(key, title, body, *, origin="from your terms", locked=False):
        # Clauses are numbered HERE, by position, not written into the
        # sentences. A term nobody set drops its sentence, and a hardcoded
        # number would leave the document reading 3.5, 3.7, 3.8 — which on a
        # contract looks like a clause went missing rather than like one was
        # never needed.
        kept = [line for line in body if line]
        if not kept:
            return
        n = len(out) + 1
        numbered = [f"{n}.{i}  {line}" for i, line in enumerate(kept, start=1)]
        out.append({"key": key, "title": title, "body": "\n".join(numbered),
                    "origin": origin, "locked": locked})

    add("parties", "Parties and cover", [
        "This Agreement is made between {{carrier_name}} (the “Carrier”) and "
        "{{counterparty_name}} (the “Broker”).",
        "Business may be declared under it from {{inception}} to "
        "{{expiry}}, both days inclusive. A risk whose effective date falls "
        "outside that period is not covered by this contract."
        if v.get("inception_dt") and v.get("expiry_dt") else "",
        "The business written under it is {{class_of_business}}, and no "
        "other class may be declared under this Agreement."
        if v.get("class_of_business") else "",
    ], origin="standard wording", locked=True)

    if has("coverage", "territory", "excluded_territory", "permitted_risks",
           "excluded_risks", "policy_period_months", "transaction_types"):
        add("cover", "Cover", [
            "The cover granted by this Agreement is {{coverage}}, and a loss "
            "of any other kind is not recoverable under it."
            if has("coverage") else "",
            "Business may be written only where the risk is situated in "
            "{{territory}}, and a risk situated elsewhere is outside this "
            "Agreement." if has("territory") else "",
            # Placed straight after the permitted territory, where a reader
            # comparing the two will be looking. A contract may state either or
            # both: "anywhere in the EU except Malta" is one of each, and it is
            # the commonest shape of all.
            "No business may be written in {{excluded_territory}}. A risk "
            "situated there is not covered by this Agreement, whether or not "
            "it would otherwise be acceptable."
            if has("excluded_territory") else "",
            "The Broker may write {{permitted_risks}} under this Agreement, "
            "and nothing of any other kind."
            if has("permitted_risks") else "",
            "No risk of the following kinds may be written under this "
            "Agreement in any circumstances: {{excluded_risks}}. No referral "
            "renders such a risk acceptable."
            if has("excluded_risks") else "",
            "No policy may be written for a period longer than "
            "{{policy_period_months}}, whether at inception or on any "
            "extension of it." if has("policy_period_months") else "",
            "The Broker may declare {{transaction_types}}. Any other "
            "kind of transaction appearing in a bordereau is not covered by "
            "this Agreement." if has("transaction_types") else "",
        ])

    if has("max_sum_insured", "aggregate_limit", "max_tiv", "min_premium",
           "deductible", "referral_threshold", "underwriting_authority",
           "premium_cap_total"):
        add("authority", "Authority and limits", [
            "The Broker binds risks on the Carrier's behalf within "
            "{{underwriting_authority}}, and refers anything beyond it to the "
            "Carrier before binding."
            if has("underwriting_authority") else "",
            "The sum insured on any one risk shall not exceed "
            "{{max_sum_insured}}." if has("max_sum_insured") else "",
            "The total insured value on any one risk shall not exceed "
            "{{max_tiv}}." if has("max_tiv") else "",
            "Total exposure under this Agreement shall not at any time "
            "exceed {{aggregate_limit}}." if has("aggregate_limit") else "",
            "The total premium written under this Agreement shall not "
            "exceed {{premium_cap_total}} for the term."
            if has("premium_cap_total") else "",
            "No risk shall be written at a premium of less than "
            "{{min_premium}}." if has("min_premium") else "",
            "Each claim shall carry a deductible of not less than "
            "{{deductible}}." if has("deductible") else "",
            "A risk with a sum insured above {{referral_threshold}} may "
            "not be bound without the Carrier's prior written agreement."
            if has("referral_threshold") else "",
        ])

    if has("commission_max_pct", "commission_pct", "brokerage_pct",
           "override_commission_pct", "profit_commission_pct", "broker_fee",
           "carrier_share_pct", "premium_basis", "currency", "tax_treatment"):
        add("financial", "Financial terms", [
            "All premium, claims and commission under this Agreement are "
            "expressed and settled in {{currency}}."
            if has("currency") else "",
            "Premium is reported to the Carrier on a {{premium_basis}} "
            "basis, and every bordereau is prepared on that basis."
            if has("premium_basis") else "",
            "The Broker shall be entitled to commission not exceeding "
            "{{commission_max_pct}} of premium on any risk declared under "
            "this Agreement." if has("commission_max_pct") else "",
            "Commission is payable to the Broker at {{commission_pct}} of "
            "the premium written on each risk declared under this Agreement."
            if has("commission_pct") else "",
            "Brokerage is payable at {{brokerage_pct}} of the premium "
            "written on each risk declared under this Agreement."
            if has("brokerage_pct") else "",
            "An override commission of {{override_commission_pct}} "
            "applies in addition to the rate above."
            if has("override_commission_pct") else "",
            "A profit commission of {{profit_commission_pct}} of "
            "underwriting profit is payable at the end of the term."
            if has("profit_commission_pct") else "",
            "A broker fee of {{broker_fee}} applies and is reported "
            "separately from premium." if has("broker_fee") else "",
            "The Carrier's share of each risk is {{carrier_share_pct}}. "
            "Premium and claims are reported at 100% and apportioned "
            "accordingly." if has("carrier_share_pct") else "",
            "Premium is reported {{tax_treatment}} on every bordereau, and "
            "any tax shown is accounted for separately from the premium itself."
            if has("tax_treatment") else "",
            "Commission shall be shown separately on every bordereau, "
            "and returned on the same basis as any premium refunded.",
        ])

    add("reporting", "Reporting and settlement", [
        "A bordereau of all business written under this Agreement is "
        "submitted each period in the agreed format.",
        "Accounts between the parties are settled {{settlement_frequency}}, "
        "and each settlement covers every risk declared in that period."
        if has("settlement_frequency") else "",
        "Premium is payable within {{payment_terms_days}} of the end of "
        "the period in which it was written."
        if has("payment_terms_days") else "",
        "Every row is checked against the terms of this Agreement on "
        "receipt, and exceptions are queried before settlement.",
    ], origin="standard wording")

    if v.get("notice_period_days"):
        add("termination", "Termination", [
            "Either party may terminate this Agreement on "
            "{{notice_period_days}}' written notice. Risks attaching before "
            "termination run to their natural expiry.",
        ])

    return out


def quoted_tokens(sections: list[dict] | None) -> set[str]:
    """Every token the wording actually quotes, across all its sections."""
    out: set[str] = set()
    for sec in (sections or []):
        if isinstance(sec, dict):
            out.update(used_tokens(sec.get("body") or ""))
    return out


def unquoted_terms(sections: list[dict] | None, limits: dict | None) -> list[str]:
    """Terms that were agreed but that no clause quotes.

    A term reaches the contract as a TOKEN, so a term whose token is nowhere in
    the wording is one of two things, and both are worth saying out loud:

      · a clause was edited and the token replaced by the number that was
        showing at the time — after which the contract keeps saying the old
        figure while the check moves with the term. This is the failure the
        chips exist to prevent and the one that gets through anyway;
      · a clause was deleted, leaving a term that is CHECKED on every row and
        stated nowhere in the document either side signed.
    """
    quoted = quoted_tokens(sections)
    return [k for k in (limits or {}) if k not in quoted]


# Not letters, digits, or the punctuation that can sit INSIDE a rendered value
# ("12,500.00", "15%"). Used to keep a re-tie off the tail of a longer number:
# "13%" must not match inside "113%".
_VALUE_EDGE_L = r"(?<![0-9A-Za-z_.,])"
_VALUE_EDGE_R = r"(?![0-9A-Za-z_%])"


def retie(sections: list[dict] | None,
          tokens: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Put a hand-typed value back on the term it is quoting.

    The editor shows every term as a chip and you cannot type one by accident —
    but you CAN delete one and type the number you were looking at, and people
    do, because it reads the same on the screen. What it is not is the same
    contract: the chip moves when the term moves and the typed number does not,
    so the wording goes on saying 13% after the commission is settled at 14%.

    So it is tied back on the way in. A value is only re-tied when exactly ONE
    term renders as that text — two terms agreed at the same number are
    genuinely ambiguous, and guessing which was meant would be worse than
    leaving the words alone. What was re-tied is returned rather than done
    quietly: it changes the text of a contract, so whoever saved it is told.

    NOT IN A CLAUSE THE USER WROTE. This repairs a chip somebody deleted out of
    a clause Kavachio generated; it has no business reaching into one they
    added themselves. Writing "the deductible is 500" in your own clause and
    having "500" silently become a term reference is the opposite of helpful —
    the sentence now moves when a term you never mentioned moves. A clause
    marked `your own words` is left exactly as typed, and a term gets into it
    only when the person writing it inserts one.
    """
    if not sections:
        return sections or [], []

    # Longest first, so "123,123%" is claimed before "23%" can take a bite out
    # of it, and drop anything short enough to appear by coincidence.
    by_text: dict[str, list[str]] = {}
    for key, shown in (tokens or {}).items():
        text = str(shown).strip()
        if len(text) >= 2:
            by_text.setdefault(text, []).append(key)
    candidates = sorted(((txt, keys[0]) for txt, keys in by_text.items()
                         if len(keys) == 1),
                        key=lambda kv: len(kv[0]), reverse=True)

    retied: list[str] = []
    out: list[dict] = []
    for sec in sections:
        if not isinstance(sec, dict):
            out.append(sec)
            continue
        if (sec.get("origin") or "").strip().lower().startswith("your own words"):
            out.append(sec)          # theirs; nothing to repair
            continue
        body = sec.get("body") or ""
        for text, key in candidates:
            pattern = _VALUE_EDGE_L + re.escape(text) + _VALUE_EDGE_R
            body, n = re.subn(pattern, "{{" + key + "}}", body)
            if n and key not in retied:
                retied.append(key)
        out.append({**sec, "body": body})
    return out, retied


def derive_checks(values: dict, limits: dict,
                  sections: list[dict] | None = None
                  ) -> tuple[list[dict], list[dict]]:
    """What this contract will have checked, and what merely looks odd.

    Each check carries the severity the carrier chose in step 1 — that choice
    is the whole reason the flow exists, so it travels with the check rather
    than being decided by anybody downstream.

    Warnings never block. Each names something that is USUALLY a slip but is
    sometimes the actual deal; refusing to save over one would make the unusual
    deal unwritable.
    """
    v, lim = values or {}, limits or {}
    tokens = token_values(values=v, limits=lim)
    checks, warnings = [], []

    # The term is a check nobody types: it comes from the dates.
    if v.get("inception_dt") and v.get("expiry_dt"):
        checks.append({
            "from": "§1.2", "expression": "effective_date within term",
            "title": "Risk dates inside the term",
            "severity": "critical",
            "detail": f"Every policy's effective date must fall between "
                      f"{v['inception_dt']} and {v['expiry_dt']}."})

    for key, entry in lim.items():
        spec = AGREED_LIMITS.get(key)
        if not spec or not spec.get("check"):
            continue
        raw = entry.get("value")
        expr = spec["check"].replace("{v}", str(raw))
        checks.append({
            "from": key, "expression": expr,
            "title": spec["question"],
            "severity": entry.get("severity") or spec.get("default_severity")
                        or "warning",
            "detail": f"{spec['question']}: {tokens.get(key, raw)}."})

    def val(k):
        return (lim.get(k) or {}).get("value")

    money_terms = any(val(k) is not None for k in
                      ("min_premium", "max_sum_insured", "premium_cap_total",
                       "broker_fee"))
    if money_terms and not val("currency"):
        warnings.append({
            "title": "No contract currency",
            "detail": "Monetary limits are set but no currency is named, so "
                      "amounts cannot be checked for being in the right one."})

    if val("commission_max_pct") and val("commission_pct"):
        if float(val("commission_pct")) > float(val("commission_max_pct")):
            warnings.append({
                "title": "Commission is above its own cap",
                "detail": f"The agreed rate ({_pct(val('commission_pct'))}) is "
                          f"higher than the cap "
                          f"({_pct(val('commission_max_pct'))}). One of the "
                          f"two is probably not what was meant."})

    # A term the document does not state. See unquoted_terms: either a clause
    # was edited and its chip replaced by the number showing at the time, or the
    # clause was deleted — and in both cases the contract and the checks have
    # stopped saying the same thing.
    for key in unquoted_terms(sections, lim) if sections is not None else []:
        spec = AGREED_LIMITS.get(key)
        if not spec:
            continue
        warnings.append({
            "title": f"The wording does not quote “{spec['question']}”",
            "detail": f"{spec['question']} is agreed at "
                      f"{tokens.get(key, (lim.get(key) or {}).get('value'))}, "
                      f"but no clause quotes it. If a clause states the figure "
                      f"in plain text it will keep saying the old one — edit "
                      f"that clause, or rewrite the wording from the terms."})

    if val("brokerage_pct") and val("broker_fee"):
        warnings.append({
            "title": "Two broker remunerations",
            "detail": "Both a brokerage percentage and a flat broker fee are "
                      "set. Some deals do carry both — worth confirming this "
                      "one does."})

    if val("min_premium") and val("max_sum_insured"):
        if float(val("min_premium")) > float(val("max_sum_insured")):
            warnings.append({
                "title": "Minimum premium exceeds the largest risk",
                "detail": "Nothing could be written that satisfies both."})

    return checks, warnings


def uncheckable_sections(sections: list[dict]) -> list[dict]:
    """Sections that will produce no check — the design's "worth a look".

    Not a fault: governing law and dispute clauses are real terms with nothing
    in a spreadsheet to compare them against. Saying so is cheaper than someone
    discovering it three months later.
    """
    out = []
    for s in sections:
        if not used_tokens(s.get("body") or ""):
            out.append({"title": s.get("title"), "key": s.get("key")})
    return out


# "4.2  All amounts…" → ("4.2", "All amounts…"). The number is set apart in
# the PDF the way it is on screen, so the two read as the same document.
_CLAUSE_NO = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$", re.S)


def estimate_pages(sections: list[dict]) -> int:
    """Body pages plus the signature page Kavachio adds. A preview figure."""
    chars = sum(len(s.get("title") or "") + len(s.get("body") or "") + 80
                for s in sections)
    return max(1, -(-chars // _CHARS_PER_PAGE)) + 1


def schedule_rows(*, values: dict, limits: dict,
                  type_label: str | None = None,
                  programme_name: str | None = None) -> list[tuple[str, list]]:
    """The contract's particulars, grouped the way they were agreed.

    A real contract is a SCHEDULE and a WORDING: the schedule states the
    numbers in a table anybody can check at a glance, the wording says what
    they mean in sentences. Kavachio holds both — the terms and the clauses
    written from them — so the download carries both, in that order. Reading
    the commission off a table beats hunting for clause 4.2, and the clause is
    still there for the case where the words matter.
    """
    lim = limits or {}
    v = values or {}
    tokens = token_values(values=v, limits=lim)

    head: list[tuple[str, str]] = []
    if type_label:
        head.append(("Type", type_label))
    if programme_name:
        head.append(("Programme", programme_name))
    for label, key in (("Class of business", "class_of_business"),
                       ("Contract reference", "schedule_key"),
                       ("Risk code", "risk_code"),
                       ("Section", "section_number"),
                       ("Year of account", "year_of_account")):
        if v.get(key):
            head.append((label, str(v[key])))
    if v.get("inception_dt") or v.get("expiry_dt"):
        head.append(("Period",
                     f"{v.get('inception_dt') or '—'} to "
                     f"{v.get('expiry_dt') or '—'}"))
    if v.get("notice_period_days"):
        head.append(("Notice period", f"{v['notice_period_days']} days"))

    out: list[tuple[str, list]] = []
    if head:
        out.append(("Particulars", head))

    for gkey, glabel, _hint in LIMIT_GROUPS:
        rows = [(AGREED_LIMITS[k]["question"], tokens[k])
                for k in lim
                if k in AGREED_LIMITS and AGREED_LIMITS[k].get("group") == gkey
                and tokens.get(k)]
        if rows:
            out.append((glabel, rows))
    return out


def compose_pdf(*, name: str | None, carrier_name: str | None,
                counterparty_name: str | None, sections: list[dict],
                tokens: dict[str, str] | None = None,
                signature_layout: dict | None = None,
                subtitle: str | None = None,
                schedule: list[tuple[str, list]] | None = None,
                signers: list[dict] | None = None,
                anchors: dict[str, str] | None = None) -> bytes:
    """The WHOLE contract as a PDF: schedule, wording, signature page.

    WHY A PDF, AND WHY NOTHING IS STORED. The wording is not a document in this
    system — it is the contract's own text, held as sections and read on the
    screen. This function exists for the one moment that is not true: somebody
    wants to send it, print it or take it to a colleague. What they get is a
    typeset PDF composed on the spot from the sections as they stand, and it is
    a DIFFERENT artefact from what is on screen — fixed, paginated, signable,
    and out of date the moment a term moves.

    Nothing is written anywhere. A stored copy would be a second version of the
    contract that stops following its terms the instant one changes, and the
    whole point of holding the wording as tokens is that it cannot drift.

    PDF and not .docx: this is for reading and signing, not for editing. The
    editable version is the screen, which is live.

    THE WHOLE CONTRACT, not one part of it. A contract is a schedule and a
    wording — the numbers in a table, and what they mean in sentences — and a
    file carrying only one of the two is not the contract. So the terms come
    first, grouped as they were agreed, and the clauses follow.

    `anchors` is what makes the composed file SIGNABLE electronically. Given
    {"carrier": "tenant:12", "counterparty": "broker:42"}, each signature block
    gets invisible tags naming the party it belongs to, and the signing round
    reads its boxes back out of the document rather than being told where they
    are — so the document stays the authority on its own layout, and a wording
    that is re-composed produces the identical boxes instead of being re-drawn
    by hand. Omitted, nothing is tagged and the file is exactly what it was:
    this is only wanted for the copy going out for signature, never for the
    draft somebody downloads to read.
    """
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import (
        KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
        TableStyle)

    tokens = tokens or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=24 * mm, rightMargin=24 * mm,
        topMargin=22 * mm, bottomMargin=22 * mm,
        title=name or "Contract",
        author=carrier_name or "Kavachio")

    ss = getSampleStyleSheet()
    # Serif throughout. A contract that arrives in a sans-serif face reads as a
    # printout of a screen; this one has to read as a document.
    title_st = ParagraphStyle(
        "ctitle", parent=ss["Title"], fontName="Times-Bold",
        fontSize=17, leading=21, spaceAfter=4, alignment=TA_CENTER)
    sub_st = ParagraphStyle(
        "csub", parent=ss["Normal"], fontName="Times-Roman", fontSize=10.5,
        leading=14, textColor="#555555", alignment=TA_CENTER, spaceAfter=16)
    head_st = ParagraphStyle(
        "chead", parent=ss["Heading2"], fontName="Times-Bold", fontSize=12,
        leading=15, spaceBefore=13, spaceAfter=5, textColor="#111111")
    body_st = ParagraphStyle(
        "cbody", parent=ss["Normal"], fontName="Times-Roman", fontSize=10.5,
        leading=15.5, alignment=TA_JUSTIFY, spaceAfter=6)
    # Clause bodies hang off their number, the way a contract is set on paper.
    clause_st = ParagraphStyle(
        "cclause", parent=body_st, leftIndent=30, firstLineIndent=-30)
    sig_st = ParagraphStyle(
        "csig", parent=ss["Normal"], fontName="Times-Roman", fontSize=9.5,
        leading=17)

    def esc(t: str) -> str:
        return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    flow: list = [
        Paragraph(esc(name or "Contract"), title_st),
        Paragraph(esc(f"{carrier_name or 'Carrier'} and "
                      f"{counterparty_name or 'Counterparty'}"), sub_st),
    ]
    if subtitle:
        flow.insert(2, Paragraph(esc(subtitle), sub_st))

    # ── the schedule ──
    if schedule:
        flow.append(Paragraph("Schedule", head_st))
        for group_label, rows in schedule:
            data = [[Paragraph(f"<b>{esc(group_label)}</b>", body_st), ""]]
            data += [[Paragraph(esc(str(k)), body_st),
                      Paragraph(esc(str(v)), body_st)] for k, v in rows]
            t = Table(data, colWidths=[62 * mm, (A4[0] - 48 * mm) - 62 * mm],
                      hAlign="LEFT")
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("SPAN", (0, 0), (1, 0)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, "#333333"),
                ("LINEBELOW", (0, 1), (-1, -2), 0.25, "#dddddd"),
            ]))
            flow.append(KeepTogether(t) if len(data) < 12 else t)
            flow.append(Spacer(1, 7))
        # The wording restates these in sentences. Said plainly, because a
        # reader who spots the same number twice should know which one governs.
        flow.append(Paragraph(
            "<i>The terms above are stated again in the clauses that follow. "
            "Where a clause states a term, the clause governs.</i>", body_st))
        flow.append(Spacer(1, 4))
        if sections:
            flow.append(Paragraph("Wording", head_st))

    for i, s in enumerate(sections, start=1):
        block: list = [Paragraph(f"{i}. &nbsp;{esc(s.get('title') or 'Section')}",
                                 head_st)]
        for para in (s.get("body") or "").split("\n"):
            if not para.strip():
                continue
            # A line quoting a term that is not set is DROPPED here, not
            # rendered. render() deliberately leaves an unresolved token
            # visible — a hole somebody can see beats a number silently lost —
            # but that is a rule for the EDITOR and the preview. A document
            # going to a broker must never contain "{{commission_max_pct}}";
            # a clause that cannot state its own number does not belong in a
            # contract at all, and the preview already showed the gap in amber
            # before anyone got here.
            if any(t not in tokens for t in used_tokens(para)):
                continue
            line = render(para, tokens).strip()
            m = _CLAUSE_NO.match(line)
            if m:
                block.append(Paragraph(
                    f"<b>{esc(m.group(1))}</b>&nbsp;&nbsp;{esc(m.group(2))}",
                    clause_st))
            else:
                block.append(Paragraph(esc(line), body_st))
        # A heading must never be the last thing on a page with its first
        # clause overleaf.
        flow.append(KeepTogether(block[:2]) if len(block) > 1 else block[0])
        flow.extend(block[2:])

    # The signature page is Kavachio's: both blocks are placed without being
    # asked for, because a contract with nowhere to sign is a draft that merely
    # looks finished.
    flow.append(PageBreak())
    flow.append(Paragraph("Signatures", head_st))
    flow.append(Paragraph(
        "Signed for and on behalf of the parties, whereby they agree to the "
        "terms set out above.", body_st))
    flow.append(Spacer(1, 16 * mm))

    # Normalised once, here, so every function below may assume the shape. A
    # row written before the block was configurable — {} or {"arrangement":
    # "stacked"} — comes back as the four lines it has always had.
    layout = esign_pdf.normalise_signature_layout(signature_layout)

    def anchor(kind: str, side: str, slot: int = 1) -> str:
        """An invisible tag that puts a signing box of `kind` right here.

        Drawn in white rather than hidden, because this is a ReportLab flowable
        and there is no text render mode to reach from paragraph markup. The
        practical difference is only that selecting the text of the page would
        reveal it — it does not print, and it is not visible on screen. The
        token has to survive in the file either way: the signing round finds
        its boxes by searching for these, and redacting them would mean a
        re-composed wording could never be re-tagged.

        `slot` is WHICH of this side's signatories the box belongs to. One is
        the base key and is what a two-signature contract has always used; a
        second or third person named for the same side gets their own key, so
        their boxes are theirs and nobody else can fill them.

        Empty when the caller asked for no anchors, which is every use of this
        function except the copy going out for signature.
        """
        key = (anchors or {}).get(side)
        if not key:
            return ""
        return ('<font color="#ffffff" size="5">'
                + "{{" + kind + ":" + esign_pdf.slot_key(key, slot) + "}}"
                + "</font>")

    def lines_under(side: str, sg: dict | None, anchored: bool,
                    slot: int) -> list[tuple[str, str]]:
        """The lines under one ruled line, as (label, what goes beside it).

        The two halves are kept APART so the block can set them in two columns.
        Run together as one string, every line started its box wherever its own
        label happened to end — "Initials:" a third of an inch left of "Date
        signed:" — and a signer saw four boxes wandering across the page.

        WHICH lines is the carrier's choice, not this function's — it reads
        `layout` and nothing else. That is the whole of what makes the block
        configurable: adding a line to esign_pdf.SIGNATURE_BLOCK_FIELDS makes it
        offerable in the form and drawable here, with no change on either side.

        `sg` is the person named for this side, when one was. Their name and
        title are PRINTED rather than left as boxes: the carrier already typed
        them, and asking the signer to type them again is a form, not a
        signature. `anchored` is false for somebody who is only printed on the
        page — see block_flow.
        """
        out: list[tuple[str, str]] = []
        for key in layout["fields"].get(side, ()):
            if key == "signature":
                continue                 # the ruled line itself, drawn by the caller
            label = f"{esign_pdf.FIELD_LABEL[key]}:"
            if key == "name" and sg:
                out.append((label, esc(sg.get("name") or "")))
            elif key == "title" and sg:
                out.append((label, esc(sg.get("role") or "")))
            else:
                out.append((label, anchor(key, side, slot) if anchored else ""))
        return out

    # ONE label column, both sides, whatever either of them chose to print.
    # Measured off the widest label actually used, in the face it is set in, so
    # the boxes down a block line up with each other and the two blocks line up
    # with one another.
    label_w = max(
        [stringWidth(f"{esign_pdf.FIELD_LABEL[k]}:", sig_st.fontName,
                     sig_st.fontSize)
         for sd in esign_pdf.SIGNATURE_SIDES
         for k in layout["fields"].get(sd, ()) if k != "signature"]
        or [0.0]) + 12

    def named_for(side: str) -> list[dict]:
        """The people named for this side, in the order they were named.

        Their position in THIS list is their slot, so the signing round and the
        page agree about whose boxes are whose — see esign_routes._named_signers,
        which filters the same list the same way.
        """
        return [sg for sg in (signers or []) if sg.get("side") == side
                and (sg.get("name") or "").strip()]

    def block_flow(party_label: str, org: str | None, side: str, width: float):
        """One side's signature block, set as a table.

        A table rather than a paragraph because the block has COLUMNS — a label
        and the thing beside it — and a paragraph has only a left margin.
        """
        # Whoever was named for this side gets their own rule, so the person
        # signing does not have to work out which of two identical lines is
        # theirs. Nobody named falls back to a blank block — the contract can
        # still be printed and signed by hand, which is how most of them are.
        named = named_for(side)
        rows: list[list] = [[Paragraph(
            f"<b>{esc(party_label)}</b><br/>{esc(org or '')}", sig_st), ""]]
        spans: list[int] = [0]
        rules: list[int] = []
        rule = "____________________________"

        def add_signer(sg: dict | None, slot: int, anchored: bool) -> None:
            # Above the rule, so a signature stamped from the anchor sits ON
            # the line rather than under it.
            sig = (anchor("signature", side, slot) + "<br/>") if anchored else ""
            rows.append([Paragraph(sig + rule, sig_st), ""])
            spans.append(len(rows) - 1)
            rules.append(len(rows) - 1)
            for label, beside in lines_under(side, sg, anchored, slot):
                rows.append([Paragraph(label, sig_st),
                             Paragraph(beside or "&nbsp;", sig_st)])

        if not named:
            add_signer(None, 1, bool(anchors))
        for i, sg in enumerate(named, start=1):
            # ANCHORED unless this person is only being printed. Somebody the
            # carrier deliberately gave no access to — an outside signatory who
            # is not to be sent a link — gets the same lines with nothing to
            # click on, and signs the printed copy.
            add_signer(sg, i, bool(anchors) and _may_sign_online(sg))

        t = Table(rows, colWidths=[label_w, max(10.0, width - label_w)],
                  hAlign="LEFT")
        t.setStyle(TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("LEFTPADDING", (0, 0), (-1, -1), 0),
             ("RIGHTPADDING", (0, 0), (-1, -1), 0),
             ("TOPPADDING", (0, 0), (-1, -1), 0),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]
            # The head and each rule run the width of the block; only the lines
            # under a rule are in two columns.
            + [("SPAN", (0, r), (1, r)) for r in spans]
            # Air above every rule but the first, so a side that sends two
            # people to sign reads as two blocks rather than one long list.
            + [("TOPPADDING", (0, r), (1, r), 14) for r in rules[1:]]))
        return t

    full_w = A4[0] - 48 * mm
    half_w = full_w / 2 - 10 * mm
    side_w = half_w if layout["arrangement"] == "side_by_side" else full_w
    carrier_block = block_flow("For the Carrier", carrier_name, "carrier", side_w)
    other_block = block_flow("For the Counterparty", counterparty_name,
                             "counterparty", side_w)

    placed = layout["arrangement"] == "placed"
    if placed:
        # The blocks are drawn afterwards, at the points the carrier dragged
        # them to — see below. The signature PAGE is still emitted, empty of
        # blocks: it carries the sentence the parties sign under, and keeping
        # it means the document has the same number of pages whichever
        # arrangement is chosen. Without that, choosing "placed" would shorten
        # the contract by a page and every position already recorded against a
        # page number would quietly point one page too far.
        flow.append(Spacer(1, 40 * mm))
    elif layout["arrangement"] == "stacked":
        flow.append(carrier_block)
        flow.append(Spacer(1, 22 * mm))
        flow.append(other_block)
    else:
        w = (A4[0] - 48 * mm) / 2
        t = Table([[carrier_block, other_block]], colWidths=[w, w])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 10 * mm),
        ]))
        flow.append(t)

    doc.build(flow)
    out = buf.getvalue()
    if not placed:
        return out
    return esign_pdf.draw_signature_blocks(out, [
        _placed_block(side, label, org, layout, signers, anchors)
        for side, label, org in (
            ("carrier", "For the Carrier", carrier_name),
            ("counterparty", "For the Counterparty", counterparty_name))
        if side in layout.get("blocks", {})
    ])


def _may_sign_online(sg: dict) -> bool:
    """Whether this named signatory gets boxes of their own, or is only printed.

    One question, and it is the carrier's to answer: somebody from outside
    Kavachio is named on plenty of contracts without ever being given a way
    into this system, and turning every typed address into a signing link would
    be this function making that call on their behalf. Said no, the person is
    printed on the page with their lines and signs the printed copy.

    NOT a question about the email address. A name with nowhere to write to
    still gets the block — it is the organisation's block, and whoever presses
    Sign for that organisation signs it. That is how every contract raised
    before signatories were nameable worked, and it still is.

    `access` missing means yes, which is what every contract raised before the
    question existed meant.
    """
    return sg.get("access") is not False


def _placed_block(side: str, party_label: str, org: str | None,
                  layout: dict, signers: list[dict] | None,
                  anchors: dict[str, str] | None) -> dict:
    """One hand-placed block, described for the drawer.

    The SAME choices the flowable version reads — which lines this side signs,
    and everybody named for it, in the order they were named — so the two
    arrangements produce the same block in different places rather than two
    different blocks. Each signatory gets their own rule and their own boxes,
    keyed by their slot, so three people signing for one side is three places
    to sign rather than three names under one line.
    """
    spot = layout["blocks"][side]
    named = [sg for sg in (signers or []) if sg.get("side") == side
             and (sg.get("name") or "").strip()]
    base = (anchors or {}).get(side)

    def group(sg: dict | None, slot: int) -> dict:
        lines: list[dict] = []
        for key in layout["fields"].get(side, ()):
            if key == "signature":
                continue                 # the ruled line itself
            label = f"{esign_pdf.FIELD_LABEL[key]}:"
            if key == "name" and sg:
                lines.append({"label": label, "value": sg.get("name") or ""})
            elif key == "title" and sg:
                lines.append({"label": label, "value": sg.get("role") or ""})
            else:
                lines.append({"label": label, "type": key})
        # Only the copy going out for signature is tagged, and only for
        # somebody who is actually being given a way to sign it; a draft
        # download, and anybody the carrier is only printing on the page, gets
        # the same lines with nothing to click on. Every field type is offered
        # the same key, exactly as the flowable version does.
        tagged = bool(base) and (sg is None or _may_sign_online(sg))
        return {
            "lines": lines,
            "anchors": ({k: esign_pdf.slot_key(base, slot) for k in
                         ("signature", "name", "title", "date", "initial")}
                        if tagged else {}),
        }

    return {
        "page": spot["page"], "x": spot["x"], "y": spot["y"],
        "width": esign_pdf.PLACED_BLOCK["width"],
        "title": party_label, "org": org,
        "signers": ([group(None, 1)] if not named
                    else [group(sg, i) for i, sg in enumerate(named, start=1)]),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  ENDORSEMENTS — amending a contract that is already running
# ═══════════════════════════════════════════════════════════════════════════
#
# An endorsement is not a small contract. It is a document ABOUT a contract,
# and it is written the opposite way round: a contract states everything, an
# endorsement states only what moved and leaves the rest explicitly alone.
# That last sentence — "all other terms remain unchanged" — is not decoration;
# it is what stops an endorsement being read as a replacement.
#
# It also does something a new contract never does: it changes what is checked
# on a contract that already has bordereaux running through it. So the delta
# is computed as BOTH wording and checks — from-value to to-value, old check to
# new check — because the carrier signing it needs to see which rows would have
# passed last month and will fail next month.


def derive_endorsement_changes(old_limits: dict, new_limits: dict) -> list[dict]:
    """What actually moved, as {key, question, from, to, check_before, ...}.

    A limit that is unchanged is not a change and is left out. Three kinds of
    move are recognised because they read differently in the wording: a value
    amended, a term added that was not agreed before, and a term removed.
    """
    old, new = old_limits or {}, new_limits or {}
    out: list[dict] = []
    for key in sorted(set(old) | set(new)):
        spec = AGREED_LIMITS.get(key)
        if not spec:
            continue
        o, n = old.get(key), new.get(key)
        ov = o.get("value") if isinstance(o, dict) else o
        nv = n.get("value") if isinstance(n, dict) else n
        osev = (o or {}).get("severity") if isinstance(o, dict) else None
        nsev = (n or {}).get("severity") if isinstance(n, dict) else None
        if ov == nv and osev == nsev:
            continue

        kind = ("added" if ov in (None, "") else
                "removed" if nv in (None, "") else "amended")
        before = token_values(values={}, limits={key: o} if o else {})
        after = token_values(values={}, limits={key: n} if n else {})
        # The currency the money terms are read in — taken from whichever side
        # of the change has one, so an amended amount is not shown bare.
        cur = ((new.get("currency") or old.get("currency") or {}) or {}).get("value")
        if spec["kind"] == "money":
            before[key] = _money(ov, cur) if ov not in (None, "") else ""
            after[key] = _money(nv, cur) if nv not in (None, "") else ""

        out.append({
            "key": key,
            "question": spec["question"],
            "kind": kind,
            "from": before.get(key, ""),
            "to": after.get(key, ""),
            "severity_from": osev,
            "severity_to": nsev,
            "check_before": (spec["check"].replace("{v}", str(ov))
                             if spec.get("check") and ov not in (None, "") else None),
            "check_after": (spec["check"].replace("{v}", str(nv))
                            if spec.get("check") and nv not in (None, "") else None),
        })
    return out


def build_endorsement_sections(*, contract_name: str | None, number: int,
                               effective_from: str | None,
                               changes: list[dict],
                               note: str | None = None) -> list[dict]:
    """The endorsement, in the shape endorsements are actually written.

    Values are baked here rather than tokenised, and that is deliberate — the
    opposite of a contract's wording. A contract's chip must follow its term
    because the term is live. An endorsement records what a term WAS and what
    it BECAME on a given date; if those numbers moved afterwards the endorsement
    would be describing a change that never happened.
    """
    if not changes:
        return []

    head = [
        f"This endorsement number {number} is made to "
        f"{contract_name or 'the Agreement'} (the “Agreement”).",
        f"It is hereby agreed that, with effect from {effective_from}, the "
        f"terms of the Agreement are amended as set out below."
        if effective_from else
        "It is hereby agreed that the terms of the Agreement are amended as "
        "set out below.",
    ]
    out = [{"key": "endorsement_head", "title": f"Endorsement {number}",
            "body": "\n".join(head), "origin": "standard wording",
            "locked": True}]

    lines = []
    for i, ch in enumerate(changes, start=1):
        if ch["kind"] == "amended":
            lines.append(f"{i}.  {ch['question']} is amended from "
                         f"{ch['from']} to {ch['to']}.")
        elif ch["kind"] == "added":
            lines.append(f"{i}.  {ch['question']} is agreed at {ch['to']}, "
                         f"where the Agreement previously stated none.")
        else:
            lines.append(f"{i}.  {ch['question']}, previously {ch['from']}, "
                         f"no longer applies.")
    out.append({"key": "endorsement_changes", "title": "Amendments",
                "body": "\n".join(lines), "origin": "from your changes"})

    if note and note.strip():
        out.append({"key": "endorsement_note", "title": "Reason",
                    "body": note.strip(), "origin": "your own words"})

    out.append({
        "key": "endorsement_tail", "title": "Everything else",
        "body": "All other terms, conditions and limits of the Agreement "
                "remain unchanged and in full force.",
        "origin": "standard wording", "locked": True})
    return out


def compose_endorsement_pdf(*, contract_name: str | None, number: int,
                            carrier_name: str | None,
                            counterparty_name: str | None,
                            effective_from: str | None,
                            sections: list[dict]) -> bytes:
    """The endorsement as a document, signature blocks included.

    UNLIKE THE WORDING, this one IS stored. An endorsement is a dated record of
    a change that both sides signed; the contract it amends is already running,
    and bordereaux have been checked against what it used to say. Regenerating
    it later from today's terms would produce a document that describes a
    change that never happened. So it is composed once and kept.
    """
    return compose_pdf(
        name=f"Endorsement {number} — {contract_name or 'Contract'}",
        subtitle=(f"Effective {effective_from}" if effective_from else None),
        carrier_name=carrier_name, counterparty_name=counterparty_name,
        sections=sections, tokens={})
