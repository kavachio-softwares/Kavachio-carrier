"""
An ENDORSEMENT uploaded on a treaty — read for what it changes, reviewed, and
applied.

An endorsement drafted in Kavachio (contract_routes.endorsement_create) already
knows its changes. One that arrives as a document — signed elsewhere, uploaded
under Documents → Endorsement — is only a file until it is read. Reading it
gives:

  terms     each term of the treaty's calculation basis it changes (cession,
            lines, a layer's attachment or limit, ceding / sliding-scale /
            profit commission), with its new value — only terms THIS treaty's
            type has (treaty_basis.spec); anything else is reported, not guessed
  clauses   every clause it adds or amends, title and text
  each with the page it was read from and the words it quotes.

Nothing is applied until the carrier confirms. Confirming makes each term a
dated version from the endorsement's effective date (source "endorsement",
linked to the document) and puts its clauses among the treaty's clauses.
Confirming again replaces what this endorsement applied before — never adds
to it.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Optional

import treaty_basis as tb

log = logging.getLogger("kavachio.treaty.endorsement")

MAX_CHARS = 80_000

PROMPT = """You read an ENDORSEMENT to a reinsurance treaty — a document that
changes some of the treaty's terms from a date. Report only what it says; use
null for anything it does not state. Never guess.

The treaty is a {type_label}. These are the terms an endorsement may change —
use ONLY these term keys:
{terms}
Layers, by index: {layers}

Value shapes:
- pct / money / int: a plain number (40% -> 40; USD 5,000,000 -> 5000000).
- sliding: {{"provisional_pct": n, "method": "linear" or "step",
             "points": [{{"loss_ratio": n, "rate": n}}, ...]}}
- profit: {{"rate_pct": n, "expense_pct": n or 0, "deficit_years": n or 0}}

The document is split into pages, each starting "=== Page N ===".

Return ONLY this JSON object:
{{
  "endorsement_number": "as written, or null",
  "effective_date": "YYYY-MM-DD or null — the date the changes take effect",
  "changes": [{{"term": "<term key>", "layer": <layer index or null>,
               "value": <the NEW value>, "page": N, "quote": "the exact words, at most 30"}}],
  "other_changes": [{{"text": "a change it makes that is none of the terms above",
                      "page": N}}],
  "clauses": [{{"title": "the clause's heading", "text": "its full text as written",
               "page": N}}]
}}

DOCUMENT:
"""


def _cr():
    import contract_routes
    return contract_routes


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _date(v) -> Optional[str]:
    s = str(v or "").strip()[:10]
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _page(v, pages: int) -> Optional[int]:
    try:
        p = int(v)
    except (TypeError, ValueError):
        return None
    return p if 1 <= p <= pages else None


def pdf(s, d) -> bytes:
    import treaty_wording
    return treaty_wording.pdf_of(_cr()._doc_bytes(d), d.filename)


def terms_spec(s, c) -> tuple[Optional[dict], list[dict]]:
    import treaty_routes
    st = treaty_routes.load_structure(s, c.id, names=False)
    return st, (tb.spec(st.get("treaty_type")) if st else [])


def clean_change(st: dict, spec: list[dict], raw: dict, pages: int) -> dict:
    """One change as read or as edited — checked against this treaty's terms.
    Raises BasisError."""
    term = str((raw or {}).get("term") or "").strip()
    sp = next((x for x in spec if x["term"] == term), None)
    if sp is None:
        raise tb.BasisError({"term": f"“{term}” is not a term of this treaty."})
    layer = raw.get("layer")
    if sp["level"] == "layer":
        try:
            layer = int(layer)
        except (TypeError, ValueError):
            raise tb.BasisError({"layer": f"Which layer does “{sp['label']}” change?"})
        if not 0 <= layer < len(st.get("layers") or []):
            raise tb.BasisError({"layer": f"The treaty has no layer {layer + 1}."})
    else:
        layer = None
    value = tb.clean_value(sp["kind"], raw.get("value"))
    return {"term": term, "layer": layer, "label": sp["label"], "kind": sp["kind"],
            "value": value, "page": _page(raw.get("page"), pages),
            "quote": (str(raw.get("quote") or "").strip()[:300] or None)}


def read(s, c, d, *, call=None) -> dict:
    """Read the endorsement's terms and clauses, each with its page. Kept on
    the document as its reading; nothing is applied."""
    from sqlalchemy.orm.attributes import flag_modified
    import treaty_types as tt
    import treaty_wording
    st, spec = terms_spec(s, c)
    if not st:
        raise LookupError("no structure")
    texts = treaty_wording.page_texts(pdf(s, d))
    if not any(t.strip() for t in texts):
        raise ValueError("no text")
    body = "\n".join(f"=== Page {i} ===\n{t}" for i, t in enumerate(texts, start=1))[:MAX_CHARS]
    terms = "\n".join(f"- {x['term']} ({x['kind']}, {x['level']}): {x['label']}" for x in spec)
    layers = ", ".join(f"{i} = {lay.get('name')}" for i, lay in enumerate(st.get("layers") or [])) or "none"
    if call is None:
        from contract_upload_services.gemini_service import call_gemini

        def call(prompt: str) -> str:
            return call_gemini(prompt, label="TREATY_ENDORSEMENT_READ", temperature=0,
                               seed=7, thinking_budget=2048, max_output_tokens=8192)
    raw = call(PROMPT.format(type_label=tt.label(st.get("treaty_type")), terms=terms,
                             layers=layers) + body)
    try:
        got = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (ValueError, TypeError):
        log.warning("endorsement %s: the model returned no JSON", d.id)
        got = {}

    changes, unread = [], []
    for ch in got.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        try:
            changes.append(clean_change(st, spec, ch, len(texts)))
        except tb.BasisError as e:
            # Not a term this treaty has, or not a sound value: shown for the
            # carrier to judge — never applied as read.
            unread.append({"text": f"{ch.get('term')}: {ch.get('value')} — {e}",
                           "page": _page(ch.get("page"), len(texts))})
    for oc in got.get("other_changes") or []:
        if isinstance(oc, dict) and str(oc.get("text") or "").strip():
            unread.append({"text": str(oc["text"]).strip()[:500],
                           "page": _page(oc.get("page"), len(texts))})
    clauses = [{"title": (str(cl.get("title") or "").strip()[:200] or "Clause"),
                "text": str(cl.get("text") or "").strip()[:8000],
                "page": _page(cl.get("page"), len(texts))}
               for cl in got.get("clauses") or []
               if isinstance(cl, dict) and str(cl.get("text") or "").strip()]
    reading = {"endorsement_number": (str(got.get("endorsement_number") or "").strip() or None),
               "effective_date": _date(got.get("effective_date")),
               "changes": changes, "other_changes": unread, "clauses": clauses,
               "pages": len(texts), "read_at": _now()}
    d.extracted = {**(d.extracted if isinstance(d.extracted, dict) else {}), "reading": reading}
    flag_modified(d, "extracted")
    return reading


def applied_versions(s, c, d) -> list:
    from db import TreatyTerm
    return (s.query(TreatyTerm)
            .filter(TreatyTerm.contract_id == c.id, TreatyTerm.document_id == d.id,
                    TreatyTerm.removed_at.is_(None))
            .order_by(TreatyTerm.id).all())


def _clause_tag(d) -> str:
    return f"endorsement:{d.id}"


def confirm(s, c, d, *, effective_date: Any, changes: list, clauses: list,
            number: Optional[str], user_id: Optional[int], who: Optional[str]) -> dict:
    """Apply the reviewed endorsement: its terms as dated versions from its
    effective date, its clauses among the treaty's. Replaces whatever this
    endorsement applied before. Raises BasisError (nothing is applied then)."""
    from sqlalchemy import text as sql
    from sqlalchemy.orm.attributes import flag_modified
    st, spec = terms_spec(s, c)
    if not st:
        raise tb.BasisError({"term": "Set out the treaty's structure first."})
    on = _date(effective_date)
    if not on and changes:
        raise tb.BasisError({"effective_date": "The date the endorsement takes effect."})
    pages = ((d.extracted or {}).get("reading") or {}).get("pages") or 9999
    clean = []
    for i, ch in enumerate(changes or []):
        try:
            clean.append(clean_change(st, spec, ch, pages))
        except tb.BasisError as e:
            raise tb.BasisError({f"changes.{i}.{k}": v for k, v in e.errors.items()})
    keys = [(x["term"], x["layer"]) for x in clean]
    if len(set(keys)) != len(keys):
        raise tb.BasisError({"changes": "Each term once — the endorsement gives it one new value."})

    # What this endorsement applied before is replaced, not added to.
    for r in applied_versions(s, c, d):
        r.removed_at = dt.datetime.now(dt.timezone.utc)
        r.removed_by_user_id = user_id
    s.flush()
    label = f"Endorsement {number}" if number else (d.filename or "Endorsement")
    ids = []
    for ch in clean:
        row = tb.add(s, c, term=ch["term"], layer=ch["layer"], value=ch["value"],
                     effective_from=on, source="endorsement", user_id=user_id, who=who,
                     document_id=d.id,
                     note=label + (f", page {ch['page']}" if ch.get("page") else ""))
        ids.append(row.id)

    s.execute(sql("DELETE FROM clauses_extracted WHERE contract_id = :cid AND created_by = :tag"),
              {"cid": c.id, "tag": _clause_tag(d)})
    kept = []
    for cl in clauses or []:
        text_ = str((cl or {}).get("text") or "").strip()
        if not text_:
            continue
        title = str(cl.get("title") or "").strip()[:200] or "Clause"
        page = _page(cl.get("page"), pages)
        s.execute(sql("""
            INSERT INTO clauses_extracted
                (contract_id, clause_type, title, text, page_number, section_header,
                 rule_generation_status, generated_rule_count, extraction_confidence,
                 created_by, updated_by)
            VALUES (:cid, 'other', :title, :text, :page, :header,
                    'pending', 0, 1.0, :tag, :tag)"""),
            {"cid": c.id, "title": title, "text": text_[:8000], "page": page,
             "header": f"{label} — {title}", "tag": _clause_tag(d)})
        kept.append({"title": title, "text": text_[:8000], "page": page})

    if on:
        d.effective_from = dt.date.fromisoformat(on)
    # Its clauses read for RULES — conditions the ceded business must meet
    # from its date (an exclusion, a territory, a maximum sum insured). A
    # failed read never stops the endorsement being applied.
    rules, rules_error = [], None
    if kept:
        import treaty_rules as trr
        text_ = "\n\n".join(f"{cl['title']}\n{cl['text']}" for cl in kept)
        try:
            rules = trr.from_endorsement(trr.extract(text_), document_id=d.id,
                                         label=label, effective_from=on)
        except Exception as e:  # noqa: BLE001
            log.warning("endorsement %s: rules could not be read: %s", d.id, e)
            rules_error = "Its clauses could not be read for rules just now — apply it again to retry."
    review = {"confirmed_at": _now(), "confirmed_by": who, "effective_date": on,
              "rules": rules, "rules_error": rules_error,
              "endorsement_number": number, "term_version_ids": ids,
              "changes": [{k: ch[k] for k in ("term", "layer", "label", "value", "page", "quote")}
                          for ch in clean],
              "clauses": kept}
    d.extracted = {**(d.extracted if isinstance(d.extracted, dict) else {}), "review": review}
    flag_modified(d, "extracted")
    return review


def payload(s, c, d) -> dict:
    """The endorsement, what was read from it, what is applied, and the terms
    it could change — each as in force just before its date."""
    import treaty_wording
    ex = d.extracted if isinstance(d.extracted, dict) else {}
    reading, review = ex.get("reading"), ex.get("review")
    on = ((review or {}).get("effective_date") or (reading or {}).get("effective_date")
          or (d.effective_from.isoformat() if d.effective_from else None))
    before = None
    if on:
        before = dt.date.fromisoformat(on) - dt.timedelta(days=1)
    st, _spec = terms_spec(s, c)
    terms = []
    if st:
        sched = tb.schedule(s, c, st)
        # No date yet, or one before the treaty starts: what applies from its
        # inception is what the endorsement changes.
        ref = before or dt.date.today()
        if c.inception_dt and ref < c.inception_dt:
            ref = c.inception_dt
        live = tb.in_force(sched, ref)
        terms = [{"term": t["term"], "layer": t["layer"], "label": t["label"],
                  "layer_name": t["layer_name"], "kind": t["kind"],
                  "value_before": (live.get((t["term"], t["layer"])) or {}).get("value")}
                 for t in sched["terms"]]
    validation = treaty_wording.validate(s, d)
    return {
        "document": {"id": d.id, "filename": d.filename, "version": d.version,
                     "effective_from": d.effective_from.isoformat() if d.effective_from else None,
                     "uploaded_at": _cr()._iso_utc(d.created_at), "has_file": bool(d.blob_ref or d.blob),
                     "validation": validation,
                     # Drafted in Kavachio: its changes were applied when it was made.
                     "drafted_here": bool(ex.get("endorsement_number")) and not reading},
        "reading": reading, "review": review,
        "rules": (review or {}).get("rules") or [],
        "rules_error": (review or {}).get("rules_error"),
        "applied": [{"id": r.id, "term": r.term, "layer": r.layer_index, "value": r.value,
                     "effective_from": r.effective_from.isoformat()}
                    for r in applied_versions(s, c, d)],
        "terms": terms, "treaty": c.name, "contract_id": c.id,
    }



def treaty_rules(s, c) -> list[dict]:
    """Every rule read from this treaty's applied endorsements — those still
    attached (a retired endorsement's rules stop with it)."""
    from db import ContractDocument
    out = []
    for d in (s.query(ContractDocument)
              .filter(ContractDocument.contract_id == c.id,
                      ContractDocument.kind == "endorsement",
                      ContractDocument.is_active.is_(True))
              .order_by(ContractDocument.id)):
        review = (d.extracted or {}).get("review") if isinstance(d.extracted, dict) else None
        out += (review or {}).get("rules") or []
    return out
