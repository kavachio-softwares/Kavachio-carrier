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
                  samples: dict[str, list[str]]) -> str:
    sample_block = json.dumps(
        {c: (samples.get(c, []) or [])[:MAX_SAMPLES] for c in input_cols},
        indent=2, default=str)
    return (
        "You map an insurance bordereaux (BDX) INPUT file's columns to the\n"
        "columns of a required OUTPUT template. For EACH output column, pick the\n"
        "single best matching INPUT column (or null if none fits).\n"
        "Use both the column name and the sample values.\n\n"
        "Return COMPACT JSON only, shape:\n"
        '  { "<output column>": {"in": "<input column or null>", "s": 0.0-1.0}, ... }\n\n'
        f"OUTPUT columns:\n{json.dumps(output_cols)}\n\n"
        f"INPUT columns with sample values:\n{sample_block}\n"
    )


def _gemini_enrich(
    output_cols: list[str], input_cols: list[str], samples: dict[str, list[str]],
) -> dict[str, dict]:
    """Ask Gemini to match the still-unmatched output columns. Best-effort —
    returns {} when no API key or on any failure."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not output_cols or not input_cols:
        return {}
    try:
        from google import genai
        from mapper import _lenient_json_loads  # reuse robust JSON recovery
        from contract_upload_services.gemini_service import invoke_with_retry
        client = genai.Client(api_key=api_key)
        # Route through the shared AI gateway → inherits retry/backoff + the
        # global concurrency/rate limiter. Keeps this module's lenient recovery.
        resp = invoke_with_retry(
            {
                "model": "gemini-2.5-flash",
                "contents": _build_prompt(output_cols, input_cols, samples),
                "config": {"response_mime_type": "application/json",
                           "max_output_tokens": 8192},
            },
            label="DirectMapper-enrich",
            gen_client=client,
        )
        raw = _lenient_json_loads((resp.text or "").strip()) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("Direct-mapper Gemini enrich failed: %s", e)
        return {}

    in_by_norm = {_norm(c): c for c in input_cols}
    out: dict[str, dict] = {}
    for out_col, payload in raw.items():
        if out_col not in output_cols or not isinstance(payload, dict):
            continue
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
    return out


def model_column_candidates(
    output_cols: list[str], input_cols: list[str], samples: dict[str, list[str]],
) -> dict[str, dict]:
    """What the model thinks each of these output columns means, if anything.

    The public door onto the enrichment above. It exists because the output-
    template builder asks the SAME question at a different moment — before a
    template exists, to work out which of a standard's published columns the
    incoming file could actually fill — and one door means one prompt, one
    retry policy and one snap-back-to-a-real-column rule for both callers.

    A PROPOSAL, never a decision: ``semantic_mapping`` is what accepts or
    refuses whatever comes back (plan section 12).
    """
    return _gemini_enrich(output_cols, input_cols, samples)


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
        })
    return out


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

        # The heuristic runs first purely to find out which columns it CANNOT
        # place — that shortlist is all the model is asked about, which keeps the
        # prompt small. Neither result is accepted here; both are evidence.
        _, _, unmatched = heuristic_match(in_cols, out_cols)
        semantic = {}
        if unmatched:
            semantic = model_column_candidates(unmatched, in_cols, in_samples)

        # The one place that decides. Confidence bar, data-type compatibility
        # and the ambiguity rule all live in semantic_mapping, so the model can
        # propose but never conclude.
        # A stored mapping is a RULE ({"kind":"copy","source":...}); the ladder
        # wants the source column. Only `copy` rules name one — a const or a
        # transform is not a column mapping and must not masquerade as a
        # confirmed one.
        prior = {
            col: rule.get("source")
            for col, rule in (existing_mapping.get(out_sheet) or {}).items()
            if isinstance(rule, dict) and rule.get("kind") == "copy" and rule.get("source")
        }
        decisions = sm.resolve_sheet(
            out_fields, in_cols, in_samples, existing=prior, semantic=semantic)

        mapping: dict[str, dict] = {}
        candidates: dict[str, list[dict]] = {}
        for d, f in zip(decisions, out_fields):
            col = f["column_name"]
            candidates[col] = [
                {"source": c["source"], "confidence": c["confidence"],
                 "kind": "copy", "method": c.get("method"),
                 "compatible": c.get("compatible", True)}
                for c in d.candidates]
            if d.mapped:
                mapping[col] = {"kind": "copy", "source": d.source}
        decisions_out[out_sheet] = [d.to_dict() for d in decisions]
        if any(d.status == sm.REVIEW_REQUIRED for d in decisions):
            log.info("sheet %r: %d of %d output columns need a look",
                     out_sheet,
                     sum(1 for d in decisions if d.status == sm.REVIEW_REQUIRED),
                     len(decisions))

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
