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

The same inputs must give the same answer. The model is asked at temperature 0
with a seed, in batches small enough to finish, and each output column's answer
is kept in ai_cache (kind ``column_candidates``) keyed on everything the prompt
showed about it and on the model — so the same caller asking again about the
same columns and file (a re-created template, a re-proposed setup) reuses the
answer instead of buying a new and possibly different one. Bordereau Setup and
the template builder show the model different shortlists and samples, so each
reuses only its own answers.

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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# Appendix 2 §2.7/§2.8 output defaults. Dependency-free module — importing it
# here does not pull SQLAlchemy or open a connection.
from bdx_defaults import with_output_default
import ai_cache  # imports db lazily, inside get/put
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

# Today's model. KAVACHIO_MODEL_COLUMN_MAPPING moves this one call to a bigger
# model without dragging every other flash caller along (gemini_service.model_for).
DEFAULT_MAPPING_MODEL = "gemini-2.5-flash"
# Part of every cached answer's key. Bump it whenever _build_prompt changes, or
# an answer to the old wording is served for the new one.
PROMPT_VERSION = "column_candidates_v1"
_CACHE_KIND = "column_candidates"


def _env_int(name: str, default: int, floor: int) -> int:
    try:
        return max(floor, int(os.getenv(name) or default))
    except ValueError:
        return default


def _batch_size() -> int:
    """Output columns per call. One call over ~150 columns ran out of room at a
    different point on every run, so the ask is split into pieces that finish."""
    return _env_int("KAVACHIO_COLUMN_MAPPING_BATCH", 40, 1)


def _thinking_budget() -> int:
    """A bounded budget rather than none: with thinking off, 2.5-flash matched
    noticeably fewer columns; unbounded, it spent the answer's tokens thinking.
    (gemini-2.5-pro refuses 0 — keep this above its floor when using it.)"""
    return _env_int("KAVACHIO_COLUMN_MAPPING_THINKING", 4096, 0)


def _max_output_tokens() -> int:
    # Thinking is drawn from the same budget as the answer (gemini_service), so
    # both are covered. A column's answer is ~30 tokens; 150 leaves room for long
    # names, and unused budget costs nothing.
    return min(65536, _thinking_budget() + 1024 + 150 * _batch_size())


def _mapping_model() -> str:
    from contract_upload_services.gemini_service import model_for
    return model_for("column_mapping", DEFAULT_MAPPING_MODEL)


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
    from contract_upload_services.gemini_service import (
        DETERMINISTIC_SEED, invoke_with_retry)
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    # Route through the shared AI gateway → inherits retry/backoff + the
    # global concurrency/rate limiter.
    resp = invoke_with_retry(
        {
            "model": _mapping_model(),
            "contents": prompt,
            "config": {"response_mime_type": "application/json",
                       "temperature": 0, "seed": DETERMINISTIC_SEED,
                       "thinking_config": {"thinking_budget": _thinking_budget()},
                       "max_output_tokens": _max_output_tokens()},
        },
        label="DirectMapper-enrich",
        gen_client=client,
    )
    return (resp.text or "").strip(), _finish_reason(resp)


def _cache_keys(asked: list[str], input_cols: list[str],
                samples: dict[str, list[str]],
                candidates: dict[str, list[dict]] | None,
                model: str, tenant_id: Any) -> dict[str, str]:
    """One ai_cache key per output column, over exactly what the prompt shows
    about it: its name, the input columns in order, their samples, its
    name-alike shortlist — plus the model, the generation settings and the
    tenant whose sample values these are."""
    from contract_upload_services.gemini_service import DETERMINISTIC_SEED
    shown = ai_cache.make_key(
        {c: (samples.get(c, []) or [])[:MAX_SAMPLES] for c in input_cols})
    config = {"thinking": _thinking_budget(), "seed": DETERMINISTIC_SEED}
    return {col: ai_cache.make_key(*ai_cache.model_scoped(
                (PROMPT_VERSION, tenant_id, col, list(input_cols), shown,
                 [c["source"] for c in (candidates or {}).get(col) or []], config),
                model, legacy_model=None))
            for col in asked}


def _ask_batch(cols: list[str], input_cols: list[str],
               samples: dict[str, list[str]],
               candidates: dict[str, list[dict]] | None) -> dict:
    """One call for one batch. Returns the raw per-column answers, whether they
    may be CACHED (the reply finished and parsed as sent) and, per unanswered
    column, why."""
    from mapper import _lenient_json_loads  # reuse robust JSON recovery
    res = {"answers": {}, "cacheable": False, "finish": None, "raised": False,
           "why": {}}
    try:
        text, finish = _ask_model(_build_prompt(cols, input_cols, samples, candidates))
    except Exception as e:  # noqa: BLE001
        why = f"{type(e).__name__}: {e}"[:300]
        log.warning("Direct-mapper Gemini enrich failed: %s", e)
        res.update(raised=True, why={c: why for c in cols})
        return res
    res["finish"] = finish
    try:
        raw = json.loads(re.sub(r"^```(?:json)?\s*", "", text).rstrip("`").strip())
        repaired = False
    except ValueError:
        raw, repaired = _lenient_json_loads(text), True
    if not isinstance(raw, dict) or not raw:
        why = "the AI returned no usable answer" + (f" (finish: {finish})" if finish else "")
        log.warning("Direct-mapper Gemini enrich: %s", why)
        res["why"] = {c: why for c in cols}
        return res
    items = list(raw.items())
    if repaired or finish != "STOP":
        # A cut-off answer's last entry is the one the cut may have landed in
        # ("Pol" for "Policy No"), so it is asked again rather than trusted.
        items = items[:-1]
    out_by_norm = {_norm(c): c for c in cols}
    for key, payload in items:
        # snap the model's key back to the output column it was asked about
        out_col = key if key in cols else out_by_norm.get(_norm(key))
        if out_col and isinstance(payload, dict):
            res["answers"].setdefault(out_col, payload)
    left_out = "the AI answer left this column out" + (
        f" (finish: {finish})" if finish else "")
    res["why"] = {c: left_out for c in cols if c not in res["answers"]}
    # Cut off (not STOP, or repaired JSON): nothing in it is cached, as any entry
    # may be the one the cut landed in. Finished but with columns left out: the
    # entries it does hold are whole, so they are cached and only the gap is
    # asked again — otherwise one column the model keeps skipping would leave
    # the rest of its batch bought fresh, and different, on every run.
    res["cacheable"] = not repaired and finish == "STOP"
    return res


def _ask_in_batches(cols: list[str], size: int, input_cols, samples,
                    candidates) -> list[dict]:
    """Every batch, formed in the order the columns were asked and returned in
    that order, however the calls interleave."""
    batches = [cols[i:i + size] for i in range(0, len(cols), size)]
    workers = min(len(batches), _env_int("KAVACHIO_COLUMN_MAPPING_PARALLEL", 4, 1))
    if workers <= 1:
        return [dict(_ask_batch(b, input_cols, samples, candidates), cols=b)
                for b in batches]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_ask_batch, b, input_cols, samples, candidates)
                   for b in batches]
        return [dict(f.result(), cols=b) for f, b in zip(futures, batches)]


def _snap_answer(out_col: str, payload: dict, input_cols: list[str],
                 in_by_norm: dict[str, str]) -> dict | None:
    """A raw answer ({"in", "s"}) as a proposal on a real input column, or None
    when it names none. The same for a fresh answer and a cached one."""
    src = payload.get("in")
    if not src:
        return None
    real = src if src in input_cols else in_by_norm.get(_norm(src))
    if not real:
        return None
    try:
        conf = float(payload.get("s", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    return {"source": real, "confidence": max(0.0, min(1.0, conf))}


def _gemini_enrich(
    output_cols: list[str], input_cols: list[str], samples: dict[str, list[str]],
    candidates: dict[str, list[dict]] | None = None,
    report: dict | None = None,
    *, tenant_id: Any = None, refresh: bool = False,
) -> dict[str, dict]:
    """Ask Gemini to match the still-open output columns. Best-effort —
    returns {} when no API key or on any failure.

    `candidates` are name-alike input columns per output column, for the model
    to verify first. `report`, when given, is filled in so a caller can tell a
    model that said "nothing fits" from one that never answered:
        status      ok | partial | failed | skipped (nothing to ask)
        error       why it failed, or why some columns went unanswered
        answered    output columns with an answer — a null answer counts
        unanswered  output columns still without one, and `reasons` per column
        finish      the model's finish reason (the first that was not STOP)
        cached      columns answered from ai_cache; `calls` model calls made
    A column missing from a reply is asked ONCE more, alone with the other
    missing ones; only answers from a reply that finished and parsed as sent
    are cached. `refresh` skips the cache
    lookup for a deliberate re-read.
    """
    report = report if report is not None else {}
    report.update(status="skipped", error=None, answered=[], finish=None,
                  unanswered=[], reasons={}, cached=0, calls=0, model=None)
    if not output_cols or not input_cols:
        return {}
    asked = list(dict.fromkeys(output_cols))
    if not os.getenv("GEMINI_API_KEY"):
        # Checked before anything else: gemini_service cannot even be imported
        # without a key.
        report.update(status="failed", error="GEMINI_API_KEY is not set",
                      unanswered=asked,
                      reasons={c: "GEMINI_API_KEY is not set" for c in asked})
        log.warning("Direct-mapper Gemini enrich skipped: GEMINI_API_KEY is not set")
        return {}
    try:
        model = _mapping_model()
        keys = _cache_keys(asked, input_cols, samples, candidates, model, tenant_id)
    except Exception as e:  # noqa: BLE001 — no SDK configured: nothing can be asked
        report.update(status="failed", error=f"{type(e).__name__}: {e}"[:300],
                      unanswered=asked)
        log.warning("Direct-mapper Gemini enrich unavailable: %s", e)
        return {}
    answers: dict[str, dict] = {}
    for col in asked:
        hit = ai_cache.get(_CACHE_KIND, keys[col], refresh=refresh)
        if isinstance(hit, dict):
            answers[col] = hit
    report.update(model=model, cached=len(answers))

    reasons: dict[str, str] = {}
    misses = [c for c in asked if c not in answers]
    if misses:
        size = _batch_size()
        results = _ask_in_batches(misses, size, input_cols, samples, candidates)
        # One more try for what a reply left out — never for a call that
        # errored, which the gateway has already retried.
        retry = [c for r in results if not r["raised"] for c in r["cols"]
                 if c not in r["answers"]]
        if retry:
            results += _ask_in_batches(retry, max(1, size // 2), input_cols,
                                       samples, candidates)
        report["calls"] = len(results)
        for r in results:
            if report["finish"] is None or report["finish"] == "STOP":
                report["finish"] = r["finish"] or report["finish"]
            for col, payload in r["answers"].items():
                answers.setdefault(col, payload)
                if r["cacheable"]:
                    ai_cache.put(_CACHE_KIND, keys[col], payload, tenant_id=tenant_id)
            reasons.update(r["why"])

    unanswered = [c for c in asked if c not in answers]
    report.update(answered=[c for c in asked if c in answers], unanswered=unanswered,
                  reasons={c: reasons.get(c) or "no response" for c in unanswered})
    if not unanswered:
        report["status"] = "ok"
    else:
        first = report["reasons"][unanswered[0]]
        report["status"] = "partial" if answers else "failed"
        report["error"] = first if not answers else (
            f"{len(unanswered)} of {len(asked)} column(s) unanswered — {first}")

    in_by_norm = {_norm(c): c for c in input_cols}
    out: dict[str, dict] = {}
    for col in asked:
        proposal = answers.get(col) and _snap_answer(col, answers[col], input_cols, in_by_norm)
        if proposal:
            out[col] = proposal
    return out


def model_column_candidates(
    output_cols: list[str], input_cols: list[str], samples: dict[str, list[str]],
    candidates: dict[str, list[dict]] | None = None,
    report: dict | None = None,
    *, tenant_id: Any = None, refresh: bool = False,
) -> dict[str, dict]:
    """What the model thinks each of these output columns means, if anything.

    The public door onto the enrichment above. It exists because the output-
    template builder asks the SAME question at a different moment — before a
    template exists, to work out which of a standard's published columns the
    incoming file could actually fill — and one door means one prompt, one
    retry policy, one answer cache and one snap-back-to-a-real-column rule for
    both callers. (One cache, not shared answers: each caller's key covers the
    shortlist and samples it showed, which differ between the two.)

    A PROPOSAL, never a decision: ``semantic_mapping`` is what accepts or
    refuses whatever comes back (plan section 12). `candidates`, `report`,
    `tenant_id` and `refresh` are optional — see ``_gemini_enrich``.
    """
    return _gemini_enrich(output_cols, input_cols, samples, candidates, report,
                          tenant_id=tenant_id, refresh=refresh)


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
            # what the column MEANS — lets resolve_sheet tell one value shown in
            # two columns from one input taken for two different things
            "canonical_field": c.get("canonical_field"),
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
    # A partial reply still answered the columns it covers.
    ok = report.get("status") in ("ok", "partial")
    reasons = report.get("reasons") or {}
    out: dict[str, dict] = {}
    for col in asked:
        if ok and col in answered:
            out[col] = {"asked": True, "ok": True}
            continue
        why = reasons.get(col) or (report.get("error") if not ok else (
            "the AI answer left this column out"
            + (f" (finish: {report['finish']})" if report.get("finish") else "")))
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
                f"field(s) sent to AI ({report.get('calls', 0)} call(s), "
                f"{report.get('cached', 0)} answered from cache)")
        why = f"AI {report.get('status')}" + (
            f" — {report['error']}" if report.get("error") else "")
        # A reply that left columns out is not "AI ok" — say so, and which.
        if report.get("unanswered"):
            why += " | unanswered: " + ", ".join(report["unanswered"][:20])
        event = {"ok": "ATTEMPT", "partial": "AI-PARTIAL"}.get(
            report.get("status"), "AI-FAILED")
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
    tenant_id: Any = None,
    refresh: bool = False,
):
    """Propose an input→output column mapping for every output sheet.

    `input_cols_by_sheet`   {input_sheet: [col, ...]}
    `routing`               direct_lane routing spec (tells us which input sheets
                            feed each output sheet)
    `samples_by_sheet`      {input_sheet: {col: [sample, ...]}} (optional, for AI)
    `tenant_id`             whose sample values these are — scopes the cached
                            model answers (optional)
    `refresh`               ask the model afresh instead of reusing a cached
                            answer — for a deliberate re-analysis

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
                                            candidates=similar, report=report,
                                            tenant_id=tenant_id, refresh=refresh)
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
