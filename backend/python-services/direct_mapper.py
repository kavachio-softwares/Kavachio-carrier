"""Direct-lane AI mapping: input columns → OUTPUT TEMPLATE columns.

Mirrors mapper.py's Gemini plumbing, but the target field set is the output
template's columns (typically 30–80), not the 850-field data model — so the AI
is more accurate and the user reviews a small, like-to-like mapping.

Two layers, so the engine is usable (and testable) without the network:
  1. heuristic_match()   normalised name matching → high-confidence `copy` rules
  2. propose_for_sheet()  fills the rest with Gemini when GEMINI_API_KEY is set,
                          otherwise returns only the heuristic mapping.

Since the semantic-mapping work, layer 1 is no longer the whole story: the
heuristic and the model are both treated as EVIDENCE, and `semantic_mapping`
decides what may be accepted without a person. A column only becomes a `copy`
rule when it clears the confidence bar AND its values suit the field's type; the
rest are recorded as decisions for review rather than quietly mapped.

In Bordereau Setup the model is a VERIFIER as well as a proposer. A column whose
name only looks like the field ("Gross Written Premium" for "Total gross written
premium") is no longer taken as placed; it is handed to the model as a candidate
and the model's confidence in the MEANING decides — 90%+ is wired up, above 80%
waits for a person, the rest stay unmapped. A model that never answered is
recorded as exactly that, never as "no match".

Output of propose_column_mapping():
  column_mapping  {output_sheet: {output_col: rule}}   (see direct_lane rule shapes)
  candidates      {output_sheet: {output_col: [{source, confidence}, ...]}}
  decisions       {output_sheet: [Decision.to_dict(), ...]}  — how each field
                  was decided, including the ones deliberately left unmapped
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

# Appendix 2 §2.7/§2.8 output defaults. Dependency-free module — importing it
# here does not pull SQLAlchemy or open a connection.
from bdx_defaults import with_output_default
import semantic_mapping as sm

log = logging.getLogger("bdx.direct_mapper")

COPY_CONFIDENCE_EXACT = 0.99
COPY_CONFIDENCE_FUZZY = 0.80
MAX_SAMPLES = 5


def _norm(s: Any) -> str:
    # Preserve meaning-bearing symbols that distinguish otherwise-identical
    # column names (e.g. "... Limit %" vs "... Limit $"). The alnum-only strip
    # below would erase them and collapse the two names into one, so a "%" column
    # could wrongly match a "$" column.
    t = str(s or "").lower().replace("%", " percent ").replace("$", " dollar ")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _tokens(s: Any) -> set[str]:
    return {t for t in _norm(s).split(" ") if t}


# Filler words in a published heading ("Sum Insured Currency (see code list)").
# Dropped before two names are compared, so filler makes names neither alike nor
# unalike.
_FILLER = frozenset({"a", "an", "and", "by", "code", "etc", "for", "in", "is",
                     "list", "of", "on", "or", "per", "see", "the", "this", "to"})


def _name_words(s: Any) -> set[str]:
    words = set()
    for t in _tokens(s):
        if t in _FILLER:
            continue
        # "Values" / "Value", "Fees" / "Fee" — a plural is the same word.
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]
        words.add(t)
    return words


def name_similarity(a: Any, b: Any) -> float:
    """How alike two column NAMES are, 0..1 — a shortlist score, never proof.

    The mean of two word-overlap measures: how much of the SHORTER name the
    longer one contains ("Gross Written Premium" sits whole inside "Total gross
    written premium"), and the Dice share of all words, which pulls the score
    back down when the longer name says a lot more. "Sum Insured Currency"
    scores high against "Sum Insured" too — which is why this only decides who
    the model is ASKED about, and the model decides what matches.
    """
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return 0.0
    shared = len(wa & wb)
    overlap = shared / min(len(wa), len(wb))
    dice = 2 * shared / (len(wa) + len(wb))
    return round((overlap + dice) / 2, 3)


def similar_columns(out_col: str, input_cols: list[str],
                    bar: float | None = None, limit: int = 3) -> list[dict]:
    """The input columns whose names score ABOVE `bar` against `out_col`, best
    first — the candidates the model is asked to verify."""
    bar = sm.candidate_similarity() if bar is None else bar
    scored = [(name_similarity(out_col, c), c) for c in input_cols]
    hits = sorted((x for x in scored if x[0] > bar), key=lambda x: -x[0])
    return [{"source": c, "similarity": s} for s, c in hits[:limit]]


def output_columns_for_sheet(output_structure: dict, sheet_name: str) -> list[str]:
    """Pull the list of column names for one sheet of an exporter.parse_template
    structure. Falls back to the first sheet if the name doesn't match."""
    sheets = (output_structure or {}).get("sheets", [])
    chosen = None
    for sh in sheets:
        if str(sh.get("sheet_name")) == str(sheet_name):
            chosen = sh
            break
    if chosen is None and sheets:
        chosen = sheets[0]
    if not chosen:
        return []
    return [str(c.get("column_name")) for c in chosen.get("columns", [])
            if c.get("column_name")]


def heuristic_match(
    input_cols: list[str], output_cols: list[str]
) -> tuple[dict[str, dict], dict[str, list[dict]], list[str]]:
    """Match output columns to input columns by normalised name.

    Returns (mapping, candidates, unmatched_output_cols).
    mapping[output_col] = {"kind": "copy", "source": input_col}
    """
    in_by_norm: dict[str, str] = {}
    for c in input_cols:
        in_by_norm.setdefault(_norm(c), c)

    # Exact (case-insensitive) name index — an identical name on both sides must
    # map to ITSELF, never to a sibling that differs only by a symbol the
    # normaliser might fold (e.g. "... Limit %" vs "... Limit $").
    in_by_exact: dict[str, str] = {}
    for c in input_cols:
        in_by_exact.setdefault(str(c).strip().lower(), c)

    mapping: dict[str, dict] = {}
    candidates: dict[str, list[dict]] = {}
    unmatched: list[str] = []

    for out_col in output_cols:
        exact = in_by_exact.get(str(out_col).strip().lower())
        if exact is not None:
            mapping[out_col] = {"kind": "copy", "source": exact}
            candidates[out_col] = [{"source": exact, "confidence": COPY_CONFIDENCE_EXACT,
                                    "kind": "copy"}]
            continue
        n = _norm(out_col)
        hit = in_by_norm.get(n)
        if hit:
            mapping[out_col] = {"kind": "copy", "source": hit}
            candidates[out_col] = [{"source": hit, "confidence": COPY_CONFIDENCE_EXACT,
                                    "kind": "copy"}]
            continue
        # token-subset fuzzy match (e.g. "Gross Premium" vs "Gross Premium (USD)")
        out_tokens = _tokens(out_col)
        best, best_score = None, 0.0
        for in_col in input_cols:
            it = _tokens(in_col)
            if not it or not out_tokens:
                continue
            overlap = len(out_tokens & it) / len(out_tokens | it)
            if overlap > best_score:
                best, best_score = in_col, overlap
        if best and best_score >= 0.6:
            mapping[out_col] = {"kind": "copy", "source": best}
            candidates[out_col] = [{"source": best,
                                    "confidence": round(COPY_CONFIDENCE_FUZZY * best_score, 3),
                                    "kind": "copy"}]
        else:
            unmatched.append(out_col)
            candidates[out_col] = []
    return mapping, candidates, unmatched


# ---- Gemini enrichment (optional) -----------------------------------------

def _build_prompt(output_cols: list[str], input_cols: list[str],
                  samples: dict[str, list[str]],
                  candidates: dict[str, list[dict]] | None = None) -> str:
    sample_block = json.dumps(
        {c: (samples.get(c, []) or [])[:MAX_SAMPLES] for c in input_cols},
        indent=2, default=str)
    shortlist = {col: [c["source"] for c in hits]
                 for col, hits in (candidates or {}).items()
                 if col in output_cols and hits}
    verify = (
        "NAME-SIMILAR INPUT columns to check first for some output columns. A\n"
        "similar name is NOT proof: choose one only if its values hold the same\n"
        "business data; otherwise pick another input column, or null.\n"
        f"{json.dumps(shortlist)}\n\n") if shortlist else ""
    return (
        "You map an insurance bordereaux (BDX) INPUT file's columns to the\n"
        "columns of a required OUTPUT template. For EACH output column, pick the\n"
        "single INPUT column that holds the SAME business data (or null if none does).\n"
        "Judge the meaning from the column name AND the sample values — an amount is\n"
        "not a percentage, a currency code or a count, however alike the names are.\n"
        "\"s\" is your confidence (0.0-1.0) that the chosen column carries exactly\n"
        "that data.\n\n"
        "Return COMPACT JSON only, with EVERY output column as a key, shape:\n"
        '  { "<output column>": {"in": "<input column or null>", "s": 0.0-1.0}, ... }\n\n'
        f"OUTPUT columns:\n{json.dumps(output_cols)}\n\n"
        f"{verify}"
        f"INPUT columns with sample values:\n{sample_block}\n"
    )


def _finish_reason(resp: Any) -> str | None:
    """Why the model stopped ("STOP", "MAX_TOKENS", …) — the first thing to know
    when an answer comes back short."""
    try:
        fr = (resp.candidates or [None])[0].finish_reason
    except Exception:  # noqa: BLE001
        return None
    return getattr(fr, "name", None) or (str(fr) if fr else None)


def _ask_model(prompt: str) -> tuple[str, str | None]:
    """The one network call: (answer text, finish reason). Raises on failure."""
    from google import genai
    from contract_upload_services.gemini_service import invoke_with_retry
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    # Route through the shared AI gateway → inherits retry/backoff + the
    # global concurrency/rate limiter.
    resp = invoke_with_retry(
        {
            "model": "gemini-2.5-flash",
            "contents": prompt,
            "config": {"response_mime_type": "application/json",
                       "max_output_tokens": 8192},
        },
        label="DirectMapper-enrich",
        gen_client=client,
    )
    return (resp.text or "").strip(), _finish_reason(resp)


def _gemini_enrich(
    output_cols: list[str], input_cols: list[str], samples: dict[str, list[str]],
    candidates: dict[str, list[dict]] | None = None,
    report: dict | None = None,
) -> dict[str, dict]:
    """Ask Gemini to match the still-open output columns. Best-effort —
    returns {} when no API key or on any failure.

    `candidates` are name-alike input columns per output column, for the model
    to verify first. `report`, when given, is filled in so a caller can tell a
    model that said "nothing fits" from one that never answered:
        status    ok | failed | skipped (nothing to ask)
        error     why it failed
        answered  output columns the answer covers — a null answer counts
        finish    the model's finish reason
    """
    report = report if report is not None else {}
    report.update(status="skipped", error=None, answered=[], finish=None)
    if not output_cols or not input_cols:
        return {}
    if not os.getenv("GEMINI_API_KEY"):
        report.update(status="failed", error="GEMINI_API_KEY is not set")
        log.warning("Direct-mapper Gemini enrich skipped: GEMINI_API_KEY is not set")
        return {}
    try:
        from mapper import _lenient_json_loads  # reuse robust JSON recovery
        text, finish = _ask_model(
            _build_prompt(output_cols, input_cols, samples, candidates))
        raw = _lenient_json_loads(text) or {}
    except Exception as e:  # noqa: BLE001
        report.update(status="failed", error=f"{type(e).__name__}: {e}"[:300])
        log.warning("Direct-mapper Gemini enrich failed: %s", e)
        return {}
    report["finish"] = finish
    if not isinstance(raw, dict) or not raw:
        report.update(status="failed", error="the AI returned no usable answer"
                      + (f" (finish: {finish})" if finish else ""))
        log.warning("Direct-mapper Gemini enrich: %s", report["error"])
        return {}

    in_by_norm = {_norm(c): c for c in input_cols}
    out_by_norm = {_norm(c): c for c in output_cols}
    answered: list[str] = []
    out: dict[str, dict] = {}
    for key, payload in raw.items():
        # snap the model's key back to the output column it was asked about
        out_col = key if key in output_cols else out_by_norm.get(_norm(key))
        if not out_col or not isinstance(payload, dict):
            continue
        answered.append(out_col)
        src = payload.get("in")
        if not src:
            continue
        # snap the model's answer back to a real input column
        real = src if src in input_cols else in_by_norm.get(_norm(src))
        if not real:
            continue
        try:
            conf = float(payload.get("s", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out[out_col] = {"source": real, "confidence": max(0.0, min(1.0, conf))}
    report.update(status="ok", answered=answered)
    return out


def model_column_candidates(
    output_cols: list[str], input_cols: list[str], samples: dict[str, list[str]],
    candidates: dict[str, list[dict]] | None = None,
    report: dict | None = None,
) -> dict[str, dict]:
    """What the model thinks each of these output columns means, if anything.

    The public door onto the enrichment above. It exists because the output-
    template builder asks the SAME question at a different moment — before a
    template exists, to work out which of a standard's published columns the
    incoming file could actually fill — and one door means one prompt, one
    retry policy and one snap-back-to-a-real-column rule for both callers.

    A PROPOSAL, never a decision: ``semantic_mapping`` is what accepts or
    refuses whatever comes back (plan section 12). `candidates` and `report`
    are optional — see ``_gemini_enrich``.
    """
    return _gemini_enrich(output_cols, input_cols, samples, candidates, report)


def _output_fields_for_sheet(output_structure: dict, sheet_name: str) -> list[dict]:
    """The output columns of one sheet, with the metadata the ladder needs.

    Falls back to the first sheet the same way `output_columns_for_sheet` does,
    and completes any column that predates the field metadata — so a template
    built before this still maps.
    """
    sheets = (output_structure or {}).get("sheets", [])
    chosen = next((sh for sh in sheets
                   if str(sh.get("sheet_name")) == str(sheet_name)), None)
    if chosen is None and sheets:
        chosen = sheets[0]
    if not chosen:
        return []
    out = []
    for c in chosen.get("columns", []):
        name = c.get("column_name")
        if not name:
            continue
        if not c.get("active", True):
            continue
        out.append({
            "column_name": name,
            "display_name": c.get("display_name") or name,
            "field_key": c.get("field_key") or _norm(name).replace(" ", "_"),
            "data_type": c.get("data_type"),
            "required": bool(c.get("required")),
            "source_type": c.get("source_type") or _infer_source_type(c),
        })
    return out


def _infer_source_type(col: dict) -> str:
    """Where a column's value comes from, for a column saved before it carried
    the setting — the template editor's own inference."""
    from output_template_fields import infer_source_type
    return infer_source_type(col)


def _ai_outcome(asked: list[str], report: dict) -> dict[str, dict]:
    """Per field: was the model asked, and did it actually answer?

    A field left out of an otherwise good answer (a reply cut off at its token
    limit, most often) failed just as much as one whose call errored — both
    are "AI didn't respond", never "no match".
    """
    answered = set(report.get("answered") or [])
    ok = report.get("status") == "ok"
    out: dict[str, dict] = {}
    for col in asked:
        if ok and col in answered:
            out[col] = {"asked": True, "ok": True}
            continue
        why = report.get("error") if not ok else (
            "the AI answer left this column out"
            + (f" (finish: {report['finish']})" if report.get("finish") else ""))
        out[col] = {"asked": True, "ok": False, "error": why or "no response"}
    return out


_EVENT = {sm.AUTO_MAPPED: "AUTO", sm.MANUALLY_CONFIRMED: "MANUAL",
          sm.REVIEW_REQUIRED: "VERIFY", sm.UNMAPPED: "UNMAPPED",
          sm.AI_UNAVAILABLE: "AI-FAILED", sm.NOT_FROM_INPUT: "NOT-INPUT"}


def _pct(v: Any) -> str:
    return "-" if v is None else f"{float(v):.0%}"


def _log_decisions(out_sheet: str, decisions: list, asked: list[str],
                   report: dict) -> None:
    """One line per field — console and pipeline_decisions.log — so any mapping
    can be traced: the field, the candidates, their name likeness, what the
    model said, the verdict and why."""
    import pipeline_log as plog
    if asked:
        what = (f"sheet {out_sheet!r}: {len(asked)} of {len(decisions)} "
                f"field(s) sent to AI in one call")
        why = f"AI {report.get('status')}" + (
            f" — {report['error']}" if report.get("error") else "")
        event = "ATTEMPT" if report.get("status") == "ok" else "AI-FAILED"
    else:
        what, why, event = (f"sheet {out_sheet!r}: {len(decisions)} field(s), "
                            f"none needed AI"), "", "ATTEMPT"
    log.info("column mapping | %s | %s", what, why)
    plog.log("MAPPING", event, what, why)
    for d in decisions:
        tried = ", ".join(
            f"{c['source']} [{c.get('method')}, name {_pct(c.get('similarity'))},"
            f" AI {_pct(c.get('ai_confidence'))}]"
            for c in d.candidates[:3]) or "none"
        what = f"{d.display_name!r} <- {d.source or d.suggestion or '-'}"
        why = (f"candidates: {tried} | name {_pct(d.similarity)} | "
               f"AI {_pct(d.ai_confidence)} | {d.reason}"
               + (f" | AI error: {d.ai_error}" if d.ai_error else ""))
        ev = _EVENT.get(d.status, d.status)
        log.info("column mapping | %s | %s | %s", ev, what, why)
        plog.log("MAPPING", ev, what, why)


def propose_column_mapping(
    input_cols_by_sheet: dict[str, list[str]],
    output_structure: dict,
    routing: dict,
    samples_by_sheet: dict[str, dict[str, list[str]]] | None = None,
    existing_mapping: dict[str, dict] | None = None,
    with_decisions: bool = False,
):
    """Propose an input→output column mapping for every output sheet.

    `input_cols_by_sheet`   {input_sheet: [col, ...]}
    `routing`               direct_lane routing spec (tells us which input sheets
                            feed each output sheet)
    `samples_by_sheet`      {input_sheet: {col: [sample, ...]}} (optional, for AI)

    Returns (column_mapping, candidates) keyed by OUTPUT sheet.
    """
    samples_by_sheet = samples_by_sheet or {}
    existing_mapping = existing_mapping or {}
    column_mapping: dict[str, dict] = {}
    candidates_out: dict[str, dict] = {}
    decisions_out: dict[str, list[dict]] = {}

    for route in (routing or {}).get("routes", []):
        out_sheet = route.get("output_sheet")
        if out_sheet is None:
            continue
        # Pool the columns + samples of every input sheet feeding this output sheet.
        in_cols: list[str] = []
        in_samples: dict[str, list[str]] = {}
        for src in route.get("sources", []):
            isheet = src.get("input_sheet")
            for c in input_cols_by_sheet.get(isheet, []):
                if c not in in_cols:
                    in_cols.append(c)
            for c, vals in (samples_by_sheet.get(isheet, {}) or {}).items():
                in_samples.setdefault(c, vals)

        out_cols = output_columns_for_sheet(output_structure, out_sheet)
        out_fields = _output_fields_for_sheet(output_structure, out_sheet)

        # A stored mapping is a RULE ({"kind":"copy","source":...}); the ladder
        # wants the source column. Only `copy` rules name one — a const or a
        # transform is not a column mapping and must not masquerade as a
        # confirmed one.
        prior = {
            col: rule.get("source")
            for col, rule in (existing_mapping.get(out_sheet) or {}).items()
            if isinstance(rule, dict) and rule.get("kind") == "copy" and rule.get("source")
        }

        # Who the model is asked about: every field configured to come from the
        # bordereau that no NAME places outright. A same-name column and a
        # mapping a person already confirmed stand as they always have. A
        # column that only LOOKS like the field is not taken as placed — it goes
        # to the model as a candidate, because a similar name is not the same
        # data ("Sum Insured" is not "Sum Insured Currency").
        _, name_hits, _ = heuristic_match(in_cols, out_cols)
        placed = {col for col, hits in name_hits.items()
                  if hits and hits[0]["confidence"] >= COPY_CONFIDENCE_EXACT}
        ask = [f["column_name"] for f in out_fields
               if in_cols
               and f["source_type"] == sm.SOURCE_BDX
               and f["column_name"] not in placed
               and prior.get(f["column_name"]) not in in_cols]
        similar: dict[str, list[dict]] = {}
        for col in ask:
            hits = similar_columns(col, in_cols)
            if hits:
                similar[col] = hits
        report: dict = {}
        semantic = (model_column_candidates(ask, in_cols, in_samples,
                                            candidates=similar, report=report)
                    if ask else {})

        # The one place that decides. Confidence bands, data-type compatibility
        # and the ambiguity rule all live in semantic_mapping, so the model can
        # propose and verify but never conclude on its own.
        decisions = sm.resolve_sheet(
            out_fields, in_cols, in_samples, existing=prior, semantic=semantic,
            threshold=sm.auto_accept_confidence(),
            review_floor=sm.review_confidence(),
            similar=similar, ai=_ai_outcome(ask, report))

        mapping: dict[str, dict] = {}
        candidates: dict[str, list[dict]] = {}
        for d, f in zip(decisions, out_fields):
            col = f["column_name"]
            candidates[col] = [
                {"source": c["source"], "confidence": c["confidence"],
                 "kind": "copy", "method": c.get("method"),
                 "compatible": c.get("compatible", True),
                 "similarity": c.get("similarity"),
                 "ai_confidence": c.get("ai_confidence")}
                for c in d.candidates]
            if d.mapped:
                mapping[col] = {"kind": "copy", "source": d.source}
        decisions_out[out_sheet] = [d.to_dict() for d in decisions]
        _log_decisions(out_sheet, decisions, ask, report)

        # Appendix 2 §2.7 / §2.8 — stamp the agreed COALESCE default onto the
        # columns those sections name, so the DELIVERED FILE carries 'Unknown' /
        # 'USD' / 0 / 'UNK' rather than a blank cell. §2.8 is explicit that this
        # belongs in the output: "Null transaction codes are not permitted in
        # output. 'UNK' is the required default."
        #
        # Done HERE, on the finished mapping, rather than at each of the four
        # places heuristic_match emits a copy rule (plus the Gemini path) — one
        # pass, no branch left behind.
        #
        # And done in the PROPOSER rather than per format: a new carrier /
        # program / contract mints a new direct_format with a fresh mapping, so
        # patching formats individually never catches up. scripts/
        # apply_output_defaults backfills the ones created before this existed.
        #
        # The default does not hide the problem — library rules 50 and 53 flag
        # the defaulted VALUES, so the broker gets a filled cell AND a Critical
        # exception saying we filled it. Dates are deliberately excluded (see
        # bdx_defaults._OUTPUT_DEFAULTS): §2.2 requires a missing effective date
        # to be a hard filter, and defaulting it would silence its own rule.
        mapping = {col: with_output_default(col, rule)
                   for col, rule in mapping.items()}

        column_mapping[out_sheet] = mapping
        candidates_out[out_sheet] = candidates

    # Two-value return by default: every existing caller unpacks a pair, and
    # this stays a drop-in for them.
    if with_decisions:
        return column_mapping, candidates_out, decisions_out
    return column_mapping, candidates_out
