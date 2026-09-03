"""Output BDX template fields — the blueprint's per-column metadata.

An output template already stores its layout in ``export_templates.structure``:
one entry per sheet, each with a ``columns`` list. A column has carried the
things GENERATION needs since the beginning — the header text, the canonical
field it copies from, a transform, a constant. What it never carried is the
things EDITING and VALIDATION need: a key that survives a rename, whether the
field may be deleted, what type it holds, where its value comes from.

This module adds exactly those, in place, on the same JSON. No new table, no
migration, and a column that predates it is completed on read — so an output
template built last month opens in the editor with everything filled in and
generates byte-for-byte what it generated before.

THE RENAME RULE. ``field_key`` is minted once, from the column's ORIGINAL
header, and never changes again. Renaming "Insurer Name" to "Carrier Name"
rewrites ``display_name`` only; ``field_key`` stays ``insurer_name`` and
``source_field`` keeps resolving. That is the whole point of having both.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from data_model import DATA_MODEL

# Where a value comes from (plan section 11). Kept as plain strings rather than
# an enum so the JSON stays readable and a future source needs no migration.
SOURCE_BDX = "BDX_DATA"          # a column of the processed bordereau
SOURCE_CONTRACT = "CONTRACT"     # a term read out of the contract
SOURCE_PARTY = "PARTY"           # the insurer / carrier record
SOURCE_BROKER = "BROKER"         # the producing broker's record
SOURCE_SYSTEM = "SYSTEM"         # generated at run time (reporting period, …)
SOURCE_CALCULATED = "CALCULATED"  # derived by a transform from other fields
SOURCE_CONSTANT = "CONSTANT"     # a fixed value set on the template

SOURCE_TYPES = (SOURCE_BDX, SOURCE_CONTRACT, SOURCE_PARTY, SOURCE_BROKER,
                SOURCE_SYSTEM, SOURCE_CALCULATED, SOURCE_CONSTANT)

DATA_TYPES = ("string", "int", "decimal", "date", "datetime", "bool", "json")

# The plan's field categories (section 9), derived from the canonical model's
# OWN table names rather than a second, hand-written field library. Longest
# prefix wins, so "claim_recovery" resolves before "claim" would.
_CATEGORY_BY_TABLE_PREFIX: tuple[tuple[str, str], ...] = (
    ("tax_or_surcharge", "Tax"),
    ("premium_", "Premium"),
    ("commission", "Premium"),
    ("accruals_booked", "Premium"),
    ("policy_fee", "Premium"),
    ("claim", "Claims"),
    ("reinsur", "Reinsurance"),
    ("treaty", "Reinsurance"),
    ("ceding_session", "Reinsurance"),
    ("fronting_arrangement", "Reinsurance"),
    ("layer", "Reinsurance"),
    ("contract", "Contract"),
    ("party", "Party"),
    ("tenant", "Party"),
    ("program", "Programme"),
    ("policy", "Policy"),
    ("coverage", "Policy"),
    ("insured_location", "Policy"),
    ("building", "Policy"),
    ("validation_", "Regulatory"),
)


def category_for(canonical_field: Optional[str]) -> str:
    """Which of the plan's field categories a canonical field belongs to."""
    entry = DATA_MODEL.get(canonical_field or "")
    table = (entry or {}).get("table", "")
    best = ("", "Other")
    for prefix, label in _CATEGORY_BY_TABLE_PREFIX:
        if table.startswith(prefix) and len(prefix) > len(best[0]):
            best = (prefix, label)
    return best[1]


def slug(text: str) -> str:
    """'Gross Premium (USD)' -> 'gross_premium_usd'. The key's one job is to be
    stable and readable; uniqueness is enforced per sheet by the caller."""
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
    return s or "field"


def _unique(key: str, taken: set[str]) -> str:
    if key not in taken:
        taken.add(key)
        return key
    n = 2
    while f"{key}_{n}" in taken:
        n += 1
    out = f"{key}_{n}"
    taken.add(out)
    return out


def infer_source_type(col: dict) -> str:
    """Where this column's value comes from, read off what the column already
    says. Order matters: a constant is a constant even if it also names a
    canonical field, and a transform makes it calculated."""
    if col.get("static_value") not in (None, ""):
        return SOURCE_CONSTANT
    if col.get("transform"):
        return SOURCE_CALCULATED
    cf = col.get("canonical_field")
    entry = DATA_MODEL.get(cf or "")
    if entry:
        table = entry.get("table", "")
        if table.startswith("party") or table == "tenant":
            return SOURCE_PARTY
        if table.startswith("contract"):
            return SOURCE_CONTRACT
        if entry.get("source") == "system":
            return SOURCE_SYSTEM
    return SOURCE_BDX


def infer_data_type(col: dict) -> str:
    """The canonical model already types every field it defines; fall back to
    string rather than guessing from samples, which would be a second, weaker
    opinion sitting next to an authoritative one."""
    entry = DATA_MODEL.get(col.get("canonical_field") or "")
    t = (entry or {}).get("type")
    return t if t in DATA_TYPES else "string"


def complete_column(col: dict, order: int, taken: set[str]) -> dict:
    """Fill in a column's editing/validation metadata WITHOUT touching anything
    generation reads. Idempotent: a column that already has a field_key keeps
    it, which is what makes this safe to run on every read."""
    if not col.get("field_key"):
        # Minted from the header the column was CREATED with. After this the
        # header is free to change and the key is not.
        col["field_key"] = _unique(slug(col.get("column_name") or ""), taken)
    else:
        taken.add(col["field_key"])
    col.setdefault("display_name", col.get("column_name") or col["field_key"])
    col.setdefault("source_type", infer_source_type(col))
    col.setdefault("data_type", infer_data_type(col))
    # Nothing is required until a standard or the user says so — inventing
    # requirements would block saves on templates that were fine yesterday.
    col.setdefault("required", False)
    col.setdefault("conditional", False)
    # Set only by a reporting standard. It is what refuses a deletion, so it is
    # deliberately NOT something the editor can turn off.
    col.setdefault("system_required", False)
    col.setdefault("active", True)
    if col.get("display_order") is None:
        col["display_order"] = order
    col.setdefault("category", category_for(col.get("canonical_field")))
    return col


def complete_structure(structure: dict) -> dict:
    """Complete every column of every sheet. Mutates and returns `structure`."""
    for sheet in (structure or {}).get("sheets") or []:
        taken: set[str] = set()
        cols = sheet.get("columns") or []
        for i, col in enumerate(cols):
            complete_column(col, i, taken)
    return structure


def apply_standard(structure: dict, standard_fields: Iterable[dict]) -> dict:
    """Stamp a reporting standard's requirements onto a parsed layout.

    The layout was sliced out of the standard's own workbook, so the columns and
    the requirement rows line up by position and by published name. Matching on
    BOTH (name first, position as the fallback) keeps it correct if a future
    workbook reorders a tab.
    """
    by_name = {str(f.get("field", "")).strip().lower(): f for f in standard_fields}
    by_order = {int(f.get("display_order", -1)): f for f in standard_fields}
    for sheet in (structure or {}).get("sheets") or []:
        for i, col in enumerate(sheet.get("columns") or []):
            name = str(col.get("column_name") or "").strip().lower()
            std = by_name.get(name) or by_order.get(i)
            if not std:
                continue
            col["standard_ref"] = std.get("ref")
            col["required"] = bool(std.get("required"))
            col["system_required"] = bool(std.get("required"))
            col["conditional"] = (
                str(std.get("requirement") or "").lower().startswith("condition"))
            if std.get("comments"):
                col["standard_note"] = std["comments"]
            if std.get("territory"):
                col["standard_territory"] = std["territory"]
    return structure


def flatten(structure: dict) -> list[dict]:
    """Every field of every sheet as one flat, editor-ready list."""
    out: list[dict] = []
    for sheet in (structure or {}).get("sheets") or []:
        for col in sheet.get("columns") or []:
            out.append({
                "sheet": sheet.get("sheet_name"),
                "column_index": col.get("column_index"),
                "field_key": col.get("field_key"),
                "display_name": col.get("display_name") or col.get("column_name"),
                "column_name": col.get("column_name"),
                "source_field": col.get("canonical_field"),
                "source_type": col.get("source_type"),
                "data_type": col.get("data_type"),
                "required": bool(col.get("required")),
                "conditional": bool(col.get("conditional")),
                "system_required": bool(col.get("system_required")),
                "default_value": col.get("static_value"),
                "transformation_rule": col.get("transform"),
                "display_order": col.get("display_order"),
                "active": col.get("active", True),
                "category": col.get("category"),
                "standard_ref": col.get("standard_ref"),
                "standard_note": col.get("standard_note"),
                # A column the CONTRACT asked for that the standard publishes
                # no column for. It is optional by construction — the standard
                # is what makes a column mandatory — and the note is the
                # contract's own wording, so the editor can answer "why is this
                # here" without going back to the PDF.
                "from_contract": bool(col.get("from_contract")),
                "contract_note": col.get("contract_note"),
                # Which column of the bordereau this field was expected to be
                # filled from, and how sure that was, when the template was
                # built from both sides. Absent on every template made before
                # that existed — which is why a screen must not read "no match"
                # as "nothing will fill this".
                "input_match": col.get("input_match"),
            })
    out.sort(key=lambda f: (str(f["sheet"] or ""), f["display_order"] or 0))
    return out


# ---------------------------------------------------------------------------
# Editing (plan section 10)
# ---------------------------------------------------------------------------

# What the editor may change. `field_key` is absent on purpose — it is the one
# thing that must survive every edit, and `column_name` is absent because
# renaming goes through `display_name` (see the module docstring).
EDITABLE = {"display_name", "source_field", "source_type", "data_type",
            "required", "conditional", "default_value", "transformation_rule",
            "display_order", "active"}

_TO_COLUMN = {"source_field": "canonical_field", "default_value": "static_value",
              "transformation_rule": "transform"}


def apply_edits(structure: dict, edits: list[dict]) -> tuple[dict, list[str]]:
    """Apply the editor's field list to a structure. Returns (structure, errors).

    Edits are addressed by (sheet, field_key) — never by position or by header —
    so reordering and renaming in the same save cannot cross wires.
    """
    errors: list[str] = []
    index: dict[tuple[str, str], dict] = {}
    for sheet in (structure or {}).get("sheets") or []:
        for col in sheet.get("columns") or []:
            index[(sheet.get("sheet_name"), col.get("field_key"))] = col

    seen: set[tuple[str, str]] = set()
    for e in edits or []:
        key = (e.get("sheet"), e.get("field_key"))
        col = index.get(key)
        if col is None:
            errors.append(f"unknown field '{e.get('field_key')}' on sheet "
                          f"'{e.get('sheet')}' — it may have been removed already")
            continue
        if key in seen:
            errors.append(f"field '{e.get('field_key')}' appears twice in the save")
            continue
        seen.add(key)
        for k, v in e.items():
            if k not in EDITABLE:
                continue
            if k == "source_type" and v not in SOURCE_TYPES:
                errors.append(f"'{e.get('field_key')}': unknown source type '{v}'")
                continue
            if k == "data_type" and v not in DATA_TYPES:
                errors.append(f"'{e.get('field_key')}': unknown data type '{v}'")
                continue
            col[_TO_COLUMN.get(k, k)] = v
        # A mandatory field the standard demands cannot be switched to optional.
        if col.get("system_required"):
            col["required"] = True

    # A field the editor did not send back was removed. Refuse to lose one the
    # standard demands — that is the plan's rule F, and it has to be enforced
    # here rather than only in the browser.
    for sheet in (structure or {}).get("sheets") or []:
        kept = []
        for col in sheet.get("columns") or []:
            key = (sheet.get("sheet_name"), col.get("field_key"))
            if key in seen or not edits:
                kept.append(col)
                continue
            if col.get("system_required"):
                errors.append(
                    f"'{col.get('display_name') or col.get('column_name')}' is "
                    f"required by the reporting standard and cannot be removed")
                kept.append(col)
                continue
            # Dropped by the user: keep the column so the sample workbook's
            # styling still lines up, but mark it inactive so generation skips it.
            col["active"] = False
            kept.append(col)
        sheet["columns"] = kept

    # Renumber from the editor's order so generation writes columns in the order
    # the user arranged (plan rule I).
    for sheet in (structure or {}).get("sheets") or []:
        cols = sorted(sheet.get("columns") or [],
                      key=lambda c: (c.get("display_order") if c.get("display_order")
                                     is not None else 10**6))
        for i, col in enumerate(cols):
            col["display_order"] = i
        sheet["columns"] = cols
    return structure, errors


def add_field(structure: dict, sheet_name: str, field: dict,
              position: Optional[int] = None) -> tuple[dict, Optional[str]]:
    """Add one user-added field to a sheet (plan rule G).

    `position` is a place in the DELIVERY order — 0 puts the new column first,
    and omitting it appends, which is what every caller did before this existed.
    It is deliberately not a `column_index`: that is the physical slot in the
    sample workbook, and a column the user just invented has no cell there. So
    the new field takes a fresh slot at the end and is placed by `display_order`
    alone, which is the number generation actually writes in.
    """
    for sheet in (structure or {}).get("sheets") or []:
        if sheet.get("sheet_name") != sheet_name:
            continue
        cols = sheet.setdefault("columns", [])
        taken = {c.get("field_key") for c in cols if c.get("field_key")}
        name = str(field.get("display_name") or "").strip()
        if not name:
            return structure, "a new field needs a name"
        col = {
            "column_index": max([c.get("column_index") or 0 for c in cols], default=-1) + 1,
            "column_name": name,
            "display_name": name,
            "field_key": _unique(slug(name), taken),
            "samples": [],
            "canonical_field": field.get("source_field"),
            "source_type": field.get("source_type") or SOURCE_BDX,
            "data_type": field.get("data_type") or "string",
            "required": bool(field.get("required")),
            "conditional": bool(field.get("conditional")),
            "system_required": False,
            "static_value": field.get("default_value"),
            "transform": field.get("transformation_rule"),
            "display_order": len(cols),
            "active": True,
        }
        col["category"] = category_for(col.get("canonical_field"))
        cols.append(col)
        if position is not None:
            # Renumber the whole sheet in its current delivery order with the
            # newcomer slotted in. Only `display_order` moves; every column
            # keeps the physical slot its styling is copied from.
            ordered = sorted(
                (c for c in cols if c is not col),
                key=lambda c: (c.get("display_order")
                               if c.get("display_order") is not None
                               else c.get("column_index") or 0))
            # `position` counts the columns a person can SEE — the active ones,
            # which is what the sheet grid draws and what "insert to the left of
            # column D" means. Removed columns are still in the list, carrying
            # their place so they can be put back, so counting them here would
            # land the new column somewhere nobody pointed at.
            live = [c for c in ordered if c.get("active", True)]
            at = max(0, min(int(position), len(live)))
            slot = ordered.index(live[at]) if at < len(live) else len(ordered)
            ordered.insert(slot, col)
            for i, c in enumerate(ordered):
                c["display_order"] = i
        return structure, None
    return structure, f"sheet '{sheet_name}' is not part of this template"


# ---------------------------------------------------------------------------
# What generation actually writes
# ---------------------------------------------------------------------------
# `column_index` is the PHYSICAL slot a column occupies in the sample workbook —
# it is how style-preserving generation finds the cell to copy fonts, widths and
# number formats from. `display_order` is the order the user wants in the OUTPUT.
# They are the same number until somebody edits the template, and these two
# helpers are what keep them from being confused afterwards.

def active_columns(sheet: dict) -> list[dict]:
    """The columns that will be written, in the order they will be written."""
    cols = [c for c in (sheet.get("columns") or []) if c.get("active", True)]
    return sorted(cols, key=lambda c: (c.get("display_order")
                                       if c.get("display_order") is not None
                                       else c.get("column_index") or 0))


def diverged_from_sample(structure: dict) -> bool:
    """Has editing moved this template away from its sample workbook's layout?

    Once a field has been removed or the order changed, the sample's physical
    columns no longer line up with what we are about to write — so copying its
    styling would put January's headings over February's numbers. Generation
    falls back to a plain workbook, which is the honest result.
    """
    for sheet in (structure or {}).get("sheets") or []:
        cols = sheet.get("columns") or []
        if any(not c.get("active", True) for c in cols):
            return True
        order = [c.get("column_index") or 0 for c in active_columns(sheet)]
        if order != sorted(order):
            return True
    return False


def header_of(col: dict) -> str:
    """The text this column carries in the DELIVERED FILE.

    A rename changes `display_name` and nothing else — `column_name` stays as
    the key the projected rows are stored under, and `field_key` stays as the
    handle the mapping hangs off. So the file shows the user's word for the
    column while everything internal keeps pointing at the same thing. That
    separation is the whole reason all three exist; without this function a
    rename would be purely decorative.
    """
    return (col.get("display_name") or col.get("column_name")
            or f"Column {(col.get('column_index') or 0) + 1}")


def is_renamed(col: dict) -> bool:
    """Has the user given this column a name of their own?"""
    dn = col.get("display_name")
    return bool(dn) and dn != col.get("column_name")
