"""Is this output BDX template fit to generate a file with?

Two checks live here, and they answer different questions.

``validate_template`` asks whether the BLUEPRINT is coherent: does it still
carry every field the reporting standard demands, does each field map to
something real, are there duplicates. It runs after generation, after every
edit, and again before a template is allowed to go active — a template with
outstanding errors never becomes the one a run uses.

``compare_with_sample`` asks whether a GENERATED FILE matches the sample the
recipient supplied: same columns, same order, same shapes. It is advisory by
design. A sample is optional (plan section 14), and a difference from it is
something a person should look at, not something that should stop a delivery.

Findings use the exception shape the rest of the platform already speaks —
``{severity, code, sheet, field, message}`` — so they render in the existing
lists without a second format to learn.
"""
from __future__ import annotations

from typing import Any, Optional

from data_model import DATA_MODEL
from output_template_fields import DATA_TYPES, SOURCE_TYPES, SOURCE_CONSTANT

try:
    from extras import is_extras_key as _is_extra_key
except Exception:  # noqa: BLE001 — validation must not depend on the extras module
    def _is_extra_key(_key: str) -> bool:
        return False

# critical -> blocks activation; warning -> shown, does not block.
CRITICAL = "critical"
WARNING = "warning"


def _finding(severity: str, code: str, message: str,
             sheet: Optional[str] = None, field: Optional[str] = None) -> dict:
    return {"severity": severity, "code": code, "message": message,
            "sheet": sheet, "field": field}


def validate_template(structure: dict, *,
                      standard_fields: Optional[list[dict]] = None,
                      contract_fields: Optional[list[dict]] = None,
                      extra_fields: Optional[set[str]] = None) -> dict:
    """Check one template structure.

    `standard_fields` is the reporting standard's own field list when the
    template was built from one — the source of every "this is mandatory"
    finding, so a template built from an uploaded sample is never told it is
    missing a field nobody asked it for.

    `contract_fields` is what the contract analysis said this contract has to
    report; missing ones are warnings, because a contract term can legitimately
    be carried by a differently-named column.

    `extra_fields` are tenant-defined canonical fields (the `extra-fields`
    feature) — they are valid mapping targets even though they are not in the
    shipped data model.
    """
    findings: list[dict] = []
    sheets = (structure or {}).get("sheets") or []
    if not sheets:
        return _result([_finding(CRITICAL, "no_sheets",
                                 "This template has no sheets, so there is nothing to generate.")])

    extra = extra_fields or set()
    seen_keys: set[tuple[str, str]] = set()

    for sheet in sheets:
        sname = sheet.get("sheet_name")
        seen_names: dict[str, int] = {}
        active_cols = 0
        for col in sheet.get("columns") or []:
            key = col.get("field_key")
            label = col.get("display_name") or col.get("column_name") or key or "(unnamed)"

            if not key:
                findings.append(_finding(
                    CRITICAL, "missing_field_key",
                    f"'{label}' has no internal key, so a rename would break its mapping.",
                    sname, label))
            elif (sname, key) in seen_keys:
                findings.append(_finding(
                    CRITICAL, "duplicate_field_key",
                    f"Two fields on '{sname}' share the internal key '{key}'.",
                    sname, label))
            else:
                seen_keys.add((sname, key))

            if not col.get("active", True):
                continue
            active_cols += 1

            norm = str(label).strip().lower()
            seen_names[norm] = seen_names.get(norm, 0) + 1

            st = col.get("source_type")
            if st and st not in SOURCE_TYPES:
                findings.append(_finding(
                    CRITICAL, "bad_source_type",
                    f"'{label}' has an unknown source type '{st}'.", sname, label))

            dt = col.get("data_type")
            if dt and dt not in DATA_TYPES:
                findings.append(_finding(
                    CRITICAL, "bad_data_type",
                    f"'{label}' has an unknown data type '{dt}'.", sname, label))

            cf = col.get("canonical_field")
            # A tenant-defined extra is a legitimate target even though it is
            # not in the shipped model — recognised by its own key prefix so a
            # canonical DB we could not reach never turns a valid template
            # into an invalid one.
            if cf and cf not in DATA_MODEL and cf not in extra \
                    and not _is_extra_key(cf):
                findings.append(_finding(
                    CRITICAL, "unknown_source_field",
                    f"'{label}' maps to '{cf}', which is not a field in the data model.",
                    sname, label))

            if st == SOURCE_CONSTANT and col.get("static_value") in (None, ""):
                findings.append(_finding(
                    CRITICAL, "constant_without_value",
                    f"'{label}' is set to a constant but no value was given.", sname, label))

            # A field the standard demands with nothing behind it produces an
            # empty mandatory column — worth saying before the file goes out.
            if col.get("required") and not cf and not col.get("transform") \
                    and col.get("static_value") in (None, ""):
                findings.append(_finding(
                    WARNING, "required_without_mapping",
                    f"'{label}' is required but nothing feeds it — it will come out empty.",
                    sname, label))

        for name, n in seen_names.items():
            if n > 1:
                findings.append(_finding(
                    CRITICAL, "duplicate_field_name",
                    f"'{name}' appears {n} times on '{sname}'.", sname, name))

        if active_cols == 0:
            findings.append(_finding(
                CRITICAL, "no_active_fields",
                f"Every field on '{sname}' has been removed.", sname))

    if standard_fields:
        findings.extend(_standard_findings(sheets, standard_fields))
    if contract_fields:
        findings.extend(_contract_findings(sheets, contract_fields))
    return _result(findings)


def _active_index(sheets: list[dict]) -> tuple[set[str], set[str]]:
    """(standard refs, lowercased names) of the fields still switched on."""
    refs, names = set(), set()
    for sheet in sheets:
        for col in sheet.get("columns") or []:
            if not col.get("active", True):
                continue
            if col.get("standard_ref"):
                refs.add(str(col["standard_ref"]))
            n = col.get("display_name") or col.get("column_name")
            if n:
                names.add(str(n).strip().lower())
    return refs, names


def _standard_findings(sheets: list[dict], standard_fields: list[dict]) -> list[dict]:
    """Every field the standard calls Mandatory has to still be there.

    Matched by the standard's own code first — that is what a rename cannot
    break — and by published name as the fallback for a field added by hand.
    """
    refs, names = _active_index(sheets)
    out: list[dict] = []
    for f in standard_fields:
        if not f.get("required"):
            continue
        ref = str(f.get("ref") or "")
        name = str(f.get("field") or "")
        if ref and ref in refs:
            continue
        if name and name.strip().lower() in names:
            continue
        out.append(_finding(
            CRITICAL, "missing_mandatory_field",
            f"'{name or ref}' is mandatory in this reporting standard and is not "
            f"in the template.", None, name or ref))
    return out


def _contract_findings(sheets: list[dict], contract_fields: list[dict]) -> list[dict]:
    """Fields the contract says must be reported. A miss is a warning: the
    contract's wording and the bordereau's column headings rarely agree
    word-for-word, so this points a person at a gap rather than blocking."""
    _, names = _active_index(sheets)
    out: list[dict] = []
    for f in contract_fields:
        name = str(f.get("field") or f.get("display_name") or "").strip()
        if not name or name.lower() in names:
            continue
        out.append(_finding(
            WARNING, "contract_field_absent",
            f"The contract expects '{name}' to be reported and no column carries it.",
            None, name))
    return out


def _result(findings: list[dict]) -> dict:
    errors = [f for f in findings if f["severity"] == CRITICAL]
    return {
        "valid": not errors,
        "error_count": len(errors),
        "warning_count": len(findings) - len(errors),
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# Sample comparison (plan section 15)
# ---------------------------------------------------------------------------

MATCHED, MISSING, UNEXPECTED, CHANGED, REVIEW = (
    "MATCHED", "MISSING", "UNEXPECTED", "CHANGED", "REQUIRES_REVIEW")


def compare_with_sample(generated_sheets: list[dict],
                        sample_structure: dict) -> dict:
    """Compare a generated file's columns against the sample the recipient gave.

    `generated_sheets` is the serializer's own shape — ``[{"sheet_name",
    "columns", "rows"}]`` — so this reads what was actually written, not what
    the template said would be written.
    """
    sample_by_sheet: dict[str, list[str]] = {}
    for sh in (sample_structure or {}).get("sheets") or []:
        # The sample was PARSED from the recipient's file, so its column_name is
        # already the heading that file carries — there is no display name to
        # prefer here, and nothing in it is ever inactive.
        sample_by_sheet[str(sh.get("sheet_name"))] = [
            str(c.get("column_name") or "") for c in (sh.get("columns") or [])
            if c.get("active", True)]

    issues: list[dict] = []
    checked = 0
    for gen in generated_sheets or []:
        sname = str(gen.get("sheet_name"))
        want = sample_by_sheet.get(sname)
        if want is None:
            issues.append({"status": UNEXPECTED, "sheet": sname, "field": None,
                           "message": f"The sample has no sheet called '{sname}'."})
            continue
        # What the file actually SAYS, which after a rename is not the same as
        # the key its values are stored under. The sample carries headings too,
        # so headings are the only thing the two sides can be compared on.
        got = [str(c) for c in (gen.get("headers") or gen.get("columns") or [])]
        checked += 1
        want_set = {w.strip().lower() for w in want}
        got_set = {g.strip().lower() for g in got}
        for w in want:
            if w.strip().lower() not in got_set:
                issues.append({"status": MISSING, "sheet": sname, "field": w,
                               "message": f"The sample has '{w}'; the generated file does not."})
        for g in got:
            if g.strip().lower() not in want_set:
                issues.append({"status": UNEXPECTED, "sheet": sname, "field": g,
                               "message": f"The generated file has '{g}'; the sample does not."})
        # Order only means something once both sides carry the same columns.
        if want_set == got_set and [w.strip().lower() for w in want] != \
                [g.strip().lower() for g in got]:
            issues.append({"status": CHANGED, "sheet": sname, "field": None,
                           "message": "The columns match but their order differs from the sample."})

    for sname in sample_by_sheet:
        if not any(str(g.get("sheet_name")) == sname for g in generated_sheets or []):
            issues.append({"status": MISSING, "sheet": sname, "field": None,
                           "message": f"The sample has a '{sname}' sheet that was not generated."})

    blocking = [i for i in issues if i["status"] in (MISSING, UNEXPECTED, CHANGED)]
    return {
        "status": MATCHED if not issues else REVIEW,
        "checked_sheets": checked,
        "issue_count": len(blocking),
        "issues": issues,
    }
