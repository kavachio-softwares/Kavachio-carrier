"""
Reading a SIGNED treaty — so the carrier uploads the slip and checks what was
read, instead of retyping what the document already says.

One model call over the document's text, asked for exactly the vocabulary
treaty_types.py defines (the five types, their terms, their layer fields), and
nothing else. What comes back is then:

  * cleaned by treaty_types.validate(strict=False) — anything not in the
    vocabulary, or not a sound value, is dropped rather than trusted;
  * matched to the carrier's own reinsurers by name. A name that matches no
    party is returned as unmatched, for the carrier to add or pick — a party
    is never created as a side effect of READING a file.

Nothing is saved here. The upload endpoint saves what the carrier confirms.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import treaty_types as tt

log = logging.getLogger("kavachio.treaty.reader")

# Long treaties are mostly boilerplate after the schedule; the schedule and the
# slip are near the front. Bounded so one huge upload cannot run up a bill.
MAX_CHARS = 120_000


def _vocabulary() -> str:
    lines = []
    for t in tt.TREATY_TYPES.values():
        fields = ", ".join(
            f"{n} ({tt.TREATY_FIELDS[n]['kind']}: {tt.TREATY_FIELDS[n]['label']})"
            for n in tt.COMMON_TREATY + t["fields"])
        layer = ", ".join(
            f"{n} ({tt.LAYER_FIELDS[n]['kind']}: {tt.LAYER_FIELDS[n]['label']})"
            for n in t["layer_fields"]) or "none"
        lines.append(f'- "{t["key"]}" = {t["label"]}. {t["blurb"]}\n'
                     f'    terms: {fields}\n    layer terms: {layer}\n'
                     f'    {"several layers allowed" if t["multi_layer"] else "exactly one layer"}')
    sel = []
    for n, f in tt.TREATY_FIELDS.items():
        if f["kind"] == "select":
            sel.append(f'{n}: one of {", ".join(o["value"] for o in f["options"])}')
    return "\n".join(lines) + "\nChoice fields: " + "; ".join(sel)


PROMPT = """You read a SIGNED reinsurance treaty (a slip, a wording, or both) and
return its key facts as JSON. Only state what the document says. If a fact is
not in the document, use null — never guess.

Treaty types and the fields each one has:
{vocab}

Rules:
- Percentages are numbers without the % sign (40% -> 40). Amounts are plain
  numbers in the treaty currency, no separators (USD 5,000,000 -> 5000000).
- A loss ratio for a stop loss goes in attachment_pct / limit_pct.
- currency is the three-letter code (USD, GBP, EUR).
- Each layer lists every reinsurer subscribing to it with its signed line as a
  percentage OF THAT LAYER. Mark exactly one reinsurer per layer as lead when the
  document names a leader; otherwise mark none.
- notice_period_days: the notice of cancellation in days (3 months -> 90).
- Dates as YYYY-MM-DD.

Return ONLY this JSON object:
{{
  "name": "the treaty's title, e.g. Property Catastrophe XL 2027",
  "inception_dt": "YYYY-MM-DD or null",
  "expiry_dt": "YYYY-MM-DD or null",
  "year_of_account": "e.g. 2027, or null",
  "notice_period_days": number or null,
  "executed_date": "YYYY-MM-DD or null",
  "treaty_type": "one of the type keys above, or null",
  "terms": {{ "<term name>": value, ... }},
  "layers": [
    {{ "name": "Layer 1", "terms": {{ "<layer term name>": value }},
       "shares": [ {{ "reinsurer": "name as written", "share_pct": number,
                     "is_lead": true|false, "slip_reference": "or null" }} ] }}
  ]
}}

DOCUMENT:
"""


def _norm(name: str) -> str:
    """A name as compared: lower case, punctuation and company suffixes off,
    so 'Munich Reinsurance Company' and 'Munich Re' meet."""
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\b(reinsurance|rueckversicherung|insurance|company|co|ltd|limited|"
               r"plc|ag|se|sa|inc|corp|corporation|the|of)\b", " ", n)
    n = re.sub(r"\bre\b", " ", n)
    return " ".join(n.split())


def match_reinsurer(name: str, parties: list[tuple[int, str]]) -> Optional[int]:
    """The carrier's reinsurer this name means, or None. Exact on the
    normalised name first, then one containing the other — but only when that
    is unambiguous, so 'Re' alone never matches everything."""
    want = _norm(name)
    if not want:
        return None
    exact = [pid for pid, pn in parties if _norm(pn) == want]
    if len(exact) == 1:
        return exact[0]
    loose = [pid for pid, pn in parties
             if _norm(pn) and (_norm(pn) in want or want in _norm(pn))]
    return loose[0] if len(loose) == 1 else None


def _date(v: Any) -> Optional[str]:
    s = str(v or "").strip()[:10]
    return s if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None


def read_treaty(text: str, parties: list[tuple[int, str]],
                *, call=None) -> dict:
    """The document's text → what the upload form is filled with.

    `call` is the model call (prompt → JSON text); injectable so tests run
    without the network."""
    if call is None:
        from contract_upload_services.gemini_service import call_gemini

        def call(prompt: str) -> str:
            return call_gemini(prompt, label="TREATY_READ", temperature=0,
                               seed=7, thinking_budget=2048,
                               max_output_tokens=8192)
    body = (text or "")[:MAX_CHARS]
    raw = call(PROMPT.format(vocab=_vocabulary()) + body)
    try:
        got = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (ValueError, TypeError):
        log.warning("treaty reader: model returned no JSON")
        got = {}

    unmatched: list[str] = []
    layers = []
    for lay in got.get("layers") or []:
        if not isinstance(lay, dict):
            continue
        shares = []
        for sh in lay.get("shares") or []:
            if not isinstance(sh, dict):
                continue
            written = str(sh.get("reinsurer") or "").strip()
            pid = match_reinsurer(written, parties)
            if pid is None and written and written not in unmatched:
                unmatched.append(written)
            shares.append({"reinsurer_party_id": pid, "reinsurer_name": written,
                           "share_pct": sh.get("share_pct"),
                           "is_lead": bool(sh.get("is_lead")),
                           "slip_reference": sh.get("slip_reference")})
        layers.append({"name": lay.get("name"), "terms": lay.get("terms") or {},
                       "shares": shares})

    # Through the vocabulary, leniently: whatever is not a field of the type,
    # or not a sound value, is dropped. Shares are kept as read (with names) so
    # unmatched reinsurers stay visible on the form.
    clean = tt.validate({"treaty_type": got.get("treaty_type"),
                         "terms": got.get("terms") or {},
                         "layers": [{**lay, "shares": []} for lay in layers]},
                        strict=False)
    if clean.get("treaty_type"):
        for i, lay in enumerate(clean["layers"]):
            lay["shares"] = layers[i]["shares"] if i < len(layers) else []
        # No leader named: the biggest line leads, which is how a slip is
        # usually read — and the carrier sees the star and can move it.
        first = clean["layers"][0] if clean["layers"] else None
        if first and first["shares"] and not any(s["is_lead"] for s in first["shares"]):
            top = max(first["shares"], key=lambda s: float(s["share_pct"] or 0))
            top["is_lead"] = True

    notice = got.get("notice_period_days")
    try:
        notice = int(notice) if notice not in (None, "") else None
    except (TypeError, ValueError):
        notice = None
    return {
        "name": (str(got.get("name") or "").strip() or None),
        "inception_dt": _date(got.get("inception_dt")),
        "expiry_dt": _date(got.get("expiry_dt")),
        "year_of_account": (str(got.get("year_of_account") or "").strip() or None),
        "notice_period_days": notice,
        "executed_date": _date(got.get("executed_date")),
        "treaty": clean if clean.get("treaty_type") else None,
        "unmatched_reinsurers": unmatched,
    }
