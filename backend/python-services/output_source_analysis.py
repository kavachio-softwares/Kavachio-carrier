"""Which output columns does this scope ACTUALLY need?

A reporting standard publishes one field list per territory, and those lists are
long — Lloyd's Singapore Risk carries dozens of columns, Australia carries a
different set again. Adopting a territory wholesale gives a template full of
columns nobody can fill: the bordereau does not carry them and the contract
never asked for them. Nothing downstream can tell the difference between "this
column is empty because the data is missing" and "this column should never have
been here", so the decision belongs at CREATION time, before the template
exists.

So a template is proposed from BOTH sides of the job:

    the reporting standard / the contract   — what the output is allowed to
                                              contain and what it must contain
    the input bordereau                     — what the incoming data can fill

and every field is answered with a plain reason. A field survives when the
standard makes it mandatory, when the contract asks for it, or when the incoming
file carries it. A field that is optional in the standard, unmentioned by the
contract and absent from the data is proposed for removal — proposed, never
removed silently: the user sees the whole list with the reasoning and unticks or
re-ticks whatever they like.

THE MATCHING IS NOT A SECOND ENGINE. Working out whether the incoming file
carries a field is exactly the question ``semantic_mapping`` already answers for
a run, so this calls it — same ladder, same alias table, same confidence bar,
same compatibility check. What is new here is only WHEN it is asked: once, up
front, to decide what the template should contain.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

import output_template_fields as otf
import semantic_mapping as sm

log = logging.getLogger("bdx.output_source_analysis")

# A workbook with more tabs than this is a workbook nobody maps by hand; the cap
# keeps one pathological upload from stalling the dialog.
_MAX_SHEETS = int(os.getenv("KAVACHIO_SOURCE_ANALYSIS_MAX_SHEETS", "12"))


def include_confidence() -> float:
    """How good a match has to be for a column to be worth PUTTING IN at all.

    Deliberately a different number from ``semantic_mapping.min_confidence``,
    because it answers a different question. That one asks "may I fill this
    column without a person looking?" and is set high on purpose. This one asks
    "is this column worth having in the template?", and the cost of the two
    mistakes is not symmetric: an extra column is dropped in the editor in a
    second, while a missing one is found after delivery and needs a new version
    of the template. So the bar for including is lower than the bar for
    trusting, and the difference is said out loud in the review list.
    """
    raw = (os.getenv("KAVACHIO_TEMPLATE_INCLUDE_CONFIDENCE") or "").strip()
    try:
        v = float(raw) if raw else 0.6
    except ValueError:
        v = 0.6
    if v > 1.0:
        v = v / 100.0
    return min(max(v, 0.0), 1.0)

# Origins that mean "the contract asked for this" — the values
# ``contract_output_fields`` stamps, plus the merge's own generic one.
CONTRACT_ORIGINS = ("contract", "contract_rule", "contract_ai")


# ---------------------------------------------------------------------------
# The input side: what the incoming bordereau actually carries
# ---------------------------------------------------------------------------

def input_layout(blob: bytes, filename: Optional[str] = None,
                 sheets: Optional[Iterable[str]] = None) -> dict:
    """Read a sample bordereau into `{columns, samples, sheets}`.

    The SAME parse a setup does — ``exporter.parse_template`` — so what this
    sees is exactly what the mapping will see later, headers and sample values
    included. Column names repeat across sheets in a lot of real bordereaux, so
    the first occurrence wins and its samples are the ones kept: two "Policy No"
    columns are one question, not two.
    """
    from exporter import parse_template

    parsed = parse_template(blob, filename=filename)
    wanted = {str(s).strip().lower() for s in (sheets or []) if str(s).strip()}
    columns: list[str] = []
    samples: dict[str, list] = {}
    read: list[str] = []
    all_sheets: list[str] = []

    for sheet in (parsed.get("sheets") or [])[:_MAX_SHEETS]:
        name = str(sheet.get("sheet_name") or "")
        all_sheets.append(name)
        if wanted and name.strip().lower() not in wanted:
            continue
        read.append(name)
        for col in sheet.get("columns") or []:
            cn = str(col.get("column_name") or "").strip()
            if not cn or cn in samples:
                continue
            columns.append(cn)
            samples[cn] = list(col.get("samples") or [])

    return {"columns": columns, "samples": samples,
            "sheets": all_sheets, "sheets_read": read}


# ---------------------------------------------------------------------------
# The join: standard/contract field <-> input column
# ---------------------------------------------------------------------------

def _out_field(f: dict) -> dict:
    """One proposed field in the shape ``semantic_mapping`` expects.

    ``column_name`` is the published heading — the name the matching is done on
    — and it is deliberately the same value as ``display_name`` here, because at
    this point nobody has renamed anything yet (plan section 4).
    """
    name = str(f.get("field") or "").strip()
    return {"field_key": f.get("field_key") or sm._norm(name),
            "display_name": name, "column_name": name,
            "data_type": f.get("data_type") or "string",
            "required": bool(f.get("required"))}


def _verdict(row: dict, d) -> dict:
    """Fold one ladder answer into the field it was asked about."""
    bar = include_confidence()
    best = next((c for c in (d.candidates or [])
                 if c.get("compatible", True)), None)
    row.update({
        "in_input": bool(d.mapped),
        # Not confident enough to fill unattended, but good enough that leaving
        # the column out of the template would be the bigger mistake.
        "likely_in_input": bool(
            not d.mapped and best
            and float(best.get("confidence") or 0.0) >= bar),
        "best_candidate": ({"source": best.get("source"),
                            "confidence": round(
                                float(best.get("confidence") or 0.0), 4)}
                           if best else None),
        "input_column": d.source,
        "mapping_method": d.method,
        "mapping_status": d.status,
        "confidence": round(d.confidence, 4),
        "mapping_reason": d.reason,
        # What else was considered, so a field the user disagrees about can be
        # argued with rather than just re-ticked (plan section 8).
        "candidates": [
            {"source": c.get("source"),
             "confidence": round(float(c.get("confidence") or 0.0), 4),
             "method": c.get("method"),
             "compatible": bool(c.get("compatible", True))}
            for c in (d.candidates or [])[:3]],
    })
    return row


def cross_reference(fields: list[dict], input_cols: list[str],
                    samples: dict[str, list], *,
                    threshold: Optional[float] = None,
                    use_model: bool = True) -> list[dict]:
    """Answer, for every proposed field, "can the incoming file fill this?".

    Returns a NEW list — the inputs are not mutated — each entry the original
    field plus the mapping verdict. When there is no input file the verdict is
    simply "not checked", which is honest and leaves the standard's own
    mandatory flags doing all the deciding.
    """
    named = [f for f in (fields or []) if str(f.get("field") or "").strip()]
    if not input_cols:
        return [dict(f, in_input=False, likely_in_input=False,
                     best_candidate=None, input_column=None,
                     mapping_method=None, mapping_status=None, confidence=0.0,
                     mapping_reason="", candidates=[]) for f in named]

    cols = list(input_cols)
    # Rung 1-4: the deterministic ones. A published standard's headings are long
    # and descriptive ("Insured Full Name, Last Name or Company Name") where a
    # real bordereau's are short ("Insured"), so these place the obvious ones
    # and, more usefully, say exactly which fields nothing simple can place.
    out = [_verdict(dict(f), sm.resolve_field(_out_field(f), cols, samples,
                                              threshold=threshold))
           for f in named]

    # Rung 5: the model, asked ONLY about what is still open — the same door
    # ``direct_mapper`` uses for a run, so a field matched here and a field
    # matched at run time were matched by the same reasoning.
    open_names = [r["field"] for r in out if not r["in_input"]]
    if use_model and open_names:
        try:
            from direct_mapper import model_column_candidates
            proposals = model_column_candidates(open_names, cols, samples) or {}
        except Exception as e:  # noqa: BLE001 — the deterministic answer stands
            log.warning("semantic candidates unavailable: %s", e)
            proposals = {}
        for i, r in enumerate(out):
            proposal = proposals.get(r["field"])
            if not proposal:
                continue
            # Re-run the WHOLE ladder with the proposal in hand rather than
            # trusting it: the confidence bar, the compatibility check and the
            # ambiguity rule all still have to pass (plan section 12).
            out[i] = _verdict(dict(named[i]), sm.resolve_field(
                _out_field(named[i]), cols, samples,
                semantic=proposal, threshold=threshold))
    return out


def fold_contract_fields(library: list[dict], contract_fields: list[dict], *,
                        use_model: bool = True,
                        threshold: Optional[float] = None
                        ) -> tuple[list[dict], dict[str, dict]]:
    """Sort the contract's field list into "already published" and "new".

    A contract says "unique market reference"; the standard publishes "Unique
    Market Reference (UMR)*". Those are one column, and matching on the exact
    string — which is all a name-keyed merge can do — makes two. The file then
    goes out with a twin beside every field the contract happened to word
    differently, and neither copy is filled properly.

    So the contract's fields are put through the SAME ladder that matches an
    output field to a bordereau column, with the published names standing in
    for the incoming columns (plan section 15 — one mapping engine, not two).
    Only a match that clears the automatic bar counts as the same column: below
    it we are guessing, and a wrong guess here quietly deletes a contractual
    requirement, which is far worse than showing a column twice.

    Returns (extras, folded): the contract's own new columns, and a map from the
    published name that absorbed a requirement to the contract field that
    landed on it — so the standard's column can carry the fact that the
    contract asks for it too.
    """
    names = [n for n in (str(f.get("field") or "").strip()
                         for f in library or []) if n]
    rows = [dict(f, field=str(f.get("field") or f.get("display_name") or "").strip())
            for f in contract_fields or []]
    rows = [r for r in rows if r["field"]]
    if not rows:
        return [], {}
    if not names:
        return rows, {}
    decided = cross_reference(rows, names, {}, threshold=threshold,
                              use_model=use_model)
    extras: list[dict] = []
    folded: dict[str, dict] = {}
    for src, d in zip(rows, decided):
        hit = d.get("input_column") if d.get("in_input") else None
        if hit:
            folded.setdefault(str(hit).strip().lower(), src)
        else:
            extras.append(src)
    return extras, folded


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def recommend(fields: list[dict], *, checked_input: bool) -> list[dict]:
    """Tick or untick each field, and say why in words a person would use.

    Deterministic on purpose (plan section 12): the model contributed candidate
    field names and candidate column matches, and this — the part that decides
    what the delivered file contains — is ordinary code with a rule anyone can
    read back.
    """
    for f in fields or []:
        reasons: list[str] = []
        origin = str(f.get("origin") or "")
        from_contract = origin in CONTRACT_ORIGINS
        if bool(f.get("required")) and not from_contract:
            reasons.append("the reporting standard marks it mandatory")
        if from_contract:
            reasons.append("the contract asks for it"
                           if not f.get("required")
                           else "the contract requires it")
        elif f.get("also_in_contract"):
            # A published column the contract asks for in its own words. It is
            # kept whatever the standard says about it — the contract is the
            # binding document, and this is the column that answers it.
            reasons.append("the contract requires it too"
                           if f.get("contract_required")
                           else "the contract asks for it too")
        if f.get("in_input"):
            reasons.append(f'your bordereau carries it as "{f["input_column"]}"')
        elif f.get("likely_in_input") and f.get("best_candidate"):
            b = f["best_candidate"]
            reasons.append(
                f'your bordereau probably carries it as "{b["source"]}" '
                f'({float(b["confidence"]):.0%}) — confirm the mapping')
        f["recommended"] = bool(reasons)
        if reasons:
            f["recommend_reason"] = "; ".join(reasons)
        elif not checked_input:
            f["recommend_reason"] = (
                "optional in the standard and not named in the contract — "
                "upload the input template to see whether your data carries it")
        elif f.get("candidates"):
            best = f["candidates"][0]
            f["recommend_reason"] = (
                f'optional, not in the contract, and the closest column '
                f'("{best["source"]}") is only a {float(best["confidence"]):.0%} match')
        else:
            f["recommend_reason"] = (
                "optional in the standard, not named in the contract, and no "
                "column in your bordereau matches it")
    return fields


def summarise(fields: list[dict], *, checked_input: bool) -> dict:
    """The counts the dialog leads with, so the user sees the shape at a glance."""
    kept = [f for f in fields if f.get("recommended")]
    return {
        "total": len(fields),
        "recommended": len(kept),
        "dropped": len(fields) - len(kept),
        "from_standard": sum(1 for f in fields
                             if str(f.get("origin") or "") not in CONTRACT_ORIGINS),
        "from_contract": sum(1 for f in fields
                             if str(f.get("origin") or "") in CONTRACT_ORIGINS),
        "matched_input": sum(1 for f in fields if f.get("in_input")),
        "likely_input": sum(1 for f in fields if f.get("likely_in_input")),
        # Kept, mandatory, and nothing in the file can fill it. This is the
        # plan's unresolved-required report (section 10) asked one step earlier
        # — while the template can still be changed instead of the run failing.
        "unfilled_required": sum(
            1 for f in kept if f.get("required")
            and not f.get("in_input") and not f.get("likely_in_input")),
        "checked_input": bool(checked_input),
    }


def keep_set(fields: Iterable[dict]) -> set[str]:
    """The kept field names, lowercased — what creation filters the layout on."""
    return {str(f.get("field") or "").strip().lower()
            for f in fields or [] if str(f.get("field") or "").strip()}


def append_contract_fields(structure: dict, fields: list[dict]) -> int:
    """Append the contract's own columns to a standard-built layout.

    A territory's published list is not a superset of what a binder has to
    report. The contract can require something the standard never publishes a
    column for — a fee split, a named limit, a scheme reference — and a
    bordereau that silently leaves it out is wrong in a way nobody downstream
    can see, because there is no empty column to notice.

    So they go in, at the end, AFTER the published columns, and always
    OPTIONAL. Optional because "required" here means the reporting standard
    refuses the file without it; a column that exists only because this
    contract asks for it must not fail the standard's own check. The contract's
    wording is kept on the column so the editor can say why it is there.
    """
    sheets = (structure or {}).get("sheets") or []
    if not sheets:
        return 0
    sheet_name = sheets[0].get("sheet_name")
    present = {str(c.get("column_name") or "").strip().lower()
               for sh in sheets for c in (sh.get("columns") or [])}
    added = 0
    for f in fields or []:
        name = str(f.get("field") or "").strip()
        if not name or str(f.get("origin") or "") not in CONTRACT_ORIGINS:
            continue
        if name.lower() in present:
            continue
        _, err = otf.add_field(structure, sheet_name, {
            "display_name": name,
            "source_field": f.get("source_field"),
            "data_type": f.get("data_type") or "string",
            "required": False,
        })
        if err:
            log.warning("contract column %r not added: %s", name, err)
            continue
        present.add(name.lower())
        added += 1
        for c in sheets[0].get("columns") or []:
            if str(c.get("column_name") or "").strip().lower() == name.lower():
                c["from_contract"] = True
                note = f.get("contract_reference") or f.get("reason")
                if note:
                    c["contract_note"] = str(note)[:400]
                break
    return added


def apply_selection(structure: dict, keep: set[str],
                    matches: Optional[dict[str, dict]] = None) -> dict:
    """Switch off the columns the user did not keep, and record the matches.

    Deactivated, NOT deleted. A template generated from a standard's workbook
    keeps that workbook as its sample, and generation copies the sample's
    styling by physical column position — so removing entries would slide every
    later column onto the wrong cell. ``active=False`` is the same mechanism the
    field editor already uses for a removal, and every writer already honours it
    through ``output_template_fields.active_columns``.

    A field the standard marks mandatory is never switched off, whatever the
    selection says.
    """
    matches = matches or {}
    # Whether the incoming bordereau was actually looked at. Derived from the
    # answers rather than passed in, because only a field that went through the
    # ladder carries a mapping status at all — and a template built with no
    # input file must not report every required column as "unresolved".
    checked = any(m.get("mapping_status") for m in matches.values())
    for sheet in (structure or {}).get("sheets") or []:
        for col in sheet.get("columns") or []:
            name = str(col.get("column_name") or "").strip()
            low = name.lower()
            if keep and low not in keep and not col.get("system_required"):
                col["active"] = False
            m = matches.get(low)
            if m and m.get("input_column"):
                # Traceability (plan section 7): what the incoming file was
                # expected to fill this from, and how sure that was, recorded on
                # the template itself rather than only in the dialog that closed.
                col["input_match"] = {
                    "column": m.get("input_column"),
                    "method": m.get("mapping_method"),
                    "confidence": m.get("confidence"),
                    "version": sm.MAPPING_VERSION,
                }
    if checked:
        # Recorded ON THE TEMPLATE, not only in the dialog that created it.
        # A required column with nothing to fill it is the finding a person
        # needs while they are looking at the template — days later, on a screen
        # with room to explain it — not a line in a modal they clicked past.
        #
        # Read off the COLUMNS, not off the proposal, and only after the
        # selection has been applied: what makes a column required is what the
        # template ends up saying, and a contract field appended to a published
        # layout goes in optional however emphatically the contract worded it.
        unresolved: list[str] = []
        for sheet in (structure or {}).get("sheets") or []:
            for col in sheet.get("columns") or []:
                if not col.get("active", True):
                    continue
                if not (col.get("required") or col.get("system_required")):
                    continue
                name = str(col.get("column_name") or "").strip()
                m = matches.get(name.lower())
                if col.get("input_match") or (m and m.get("likely_in_input")):
                    continue
                if name:
                    unresolved.append(name)
        structure["source_check"] = {
            "checked_input": True,
            "unresolved_required": sorted(set(unresolved)),
            "version": sm.MAPPING_VERSION,
        }
    return structure
