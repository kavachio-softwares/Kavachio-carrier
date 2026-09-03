"""Business segments — the carrier's own list, used when creating a programme.

WHY THIS EXISTS
The programme form offered five hard-coded segments ("Casualty", "Property",
"Specialty Property", "Marine", "Financial Lines") while `program.
program_business_segment` is a free TEXT column. A carrier that writes anything
outside those five had no way to say so, and contract extraction meanwhile
writes whole clauses into the same column (see business_segment.py). So the
value was neither constrained nor the carrier's to control.

WHERE IT LIVES
`ref_code_list` + `ref_code_value` — the reference-data pair the model already
carries for exactly this: a named, tenant-scoped, versioned code list. One list
row per carrier, `list_name = 'business_segment'`, one value row per segment.
Nothing new was invented, and a global list (tenant_id IS NULL) seeded by
Kavachio would be picked up here for free.

WHICH COLUMNS
These two tables are PROTECTED (drop_superseded_columns.py) and therefore still
carry both generations of column. The primary keys, the sequences and every NOT
NULL sit on the ORIGINAL names — list_id/value_id/code/is_active — so those are
what the writes use. Using the v4 names here would insert NULL primary keys.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app_routes import resolve_tenant_id
from auth_deps import Principal, current_principal, require_role
from db import SessionLocal

router = APIRouter(tags=["segments"])

LIST_NAME = "business_segment"

# What a brand-new carrier starts with. The same five the form used to hard-code,
# so nothing an existing programme already says stops being selectable — the
# carrier can then add, rename by adding, or retire any of them.
DEFAULT_SEGMENTS = ("Casualty", "Property", "Specialty Property",
                    "Marine", "Financial Lines")

_FAR_FUTURE = datetime(2999, 12, 31)


class SegmentBody(BaseModel):
    name: str


def _list_id(s, tenant_id: int, create: bool = False) -> Optional[int]:
    """The carrier's segment list, optionally creating it on first use."""
    row = s.execute(
        text("SELECT list_id FROM ref_code_list "
             "WHERE list_name = :n AND tenant_id = :t "
             "  AND COALESCE(is_active, TRUE) "
             "ORDER BY list_id LIMIT 1"),
        {"n": LIST_NAME, "t": tenant_id},
    ).scalar()
    if row is not None or not create:
        return row
    now = datetime.utcnow()
    return s.execute(
        text("INSERT INTO ref_code_list "
             "  (tenant_id, list_name, description, is_active, created_at, "
             "   modified_at, is_current_version, valid_from, valid_until) "
             "VALUES (:t, :n, :d, TRUE, :now, :now, TRUE, :now, :far) "
             "RETURNING list_id"),
        {"t": tenant_id, "n": LIST_NAME, "now": now, "far": _FAR_FUTURE,
         "d": "Business segments this carrier writes"},
    ).scalar()


def _seed(s, list_id: int) -> None:
    """Give a new list the five the form used to hard-code."""
    now = datetime.utcnow()
    for i, name in enumerate(DEFAULT_SEGMENTS):
        s.execute(
            text("INSERT INTO ref_code_value "
                 "  (list_id, code, description, sort_order, is_active, "
                 "   created_at, modified_at, is_current_version, valid_from, valid_until) "
                 "VALUES (:l, :c, :c, :o, TRUE, :now, :now, TRUE, :now, :far)"),
            {"l": list_id, "c": name, "o": i, "now": now, "far": _FAR_FUTURE},
        )


@router.get("/business-segments")
def segments_list(mga: Optional[str] = None,
                  principal: Principal = Depends(current_principal)):
    """The segments this carrier can assign to a programme.

    Seeds the default five the first time it is asked, so an existing tenant
    sees exactly what the old hard-coded dropdown offered rather than an empty
    list it has to populate before it can create anything.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        lid = _list_id(s, tid, create=True)
        existing = s.execute(
            text("SELECT count(*) FROM ref_code_value WHERE list_id = :l"),
            {"l": lid}).scalar() or 0
        if existing == 0:
            _seed(s, lid)
        s.commit()
        rows = s.execute(
            text("SELECT value_id, code FROM ref_code_value "
                 "WHERE list_id = :l AND COALESCE(is_active, TRUE) "
                 "ORDER BY sort_order NULLS LAST, code"),
            {"l": lid}).fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]


@router.post("/business-segments")
def segments_create(body: SegmentBody,
                    mga: Optional[str] = None,
                    principal: Principal = Depends(require_role("carrier_admin"))):
    """Add a segment. Names are unique per carrier, compared case-insensitively
    so "Marine" and "marine" cannot both exist and silently split a book."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(422, "name required")
    if len(name) > 120:
        raise HTTPException(422, "name is too long")
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        lid = _list_id(s, tid, create=True)
        clash = s.execute(
            text("SELECT value_id, COALESCE(is_active, TRUE) FROM ref_code_value "
                 "WHERE list_id = :l AND lower(code) = lower(:c) LIMIT 1"),
            {"l": lid, "c": name}).fetchone()
        if clash and clash[1]:
            raise HTTPException(409, f"“{name}” is already one of your segments")
        now = datetime.utcnow()
        if clash:
            # Retired earlier, now wanted again: revive rather than duplicate,
            # so programmes that still reference it keep matching.
            s.execute(text("UPDATE ref_code_value SET is_active = TRUE, "
                           "modified_at = :now WHERE value_id = :v"),
                      {"v": clash[0], "now": now})
            vid = clash[0]
        else:
            nxt = s.execute(
                text("SELECT COALESCE(max(sort_order), -1) + 1 FROM ref_code_value "
                     "WHERE list_id = :l"), {"l": lid}).scalar()
            vid = s.execute(
                text("INSERT INTO ref_code_value "
                     "  (list_id, code, description, sort_order, is_active, "
                     "   created_at, modified_at, is_current_version, valid_from, valid_until) "
                     "VALUES (:l, :c, :c, :o, TRUE, :now, :now, TRUE, :now, :far) "
                     "RETURNING value_id"),
                {"l": lid, "c": name, "o": nxt, "now": now, "far": _FAR_FUTURE}).scalar()
        s.commit()
        return {"id": vid, "name": name}


@router.delete("/business-segments/{value_id}")
def segments_retire(value_id: int,
                    mga: Optional[str] = None,
                    principal: Principal = Depends(require_role("carrier_admin"))):
    """Retire a segment.

    Deactivated, never deleted: `program.program_business_segment` stores the
    NAME, so removing the row would leave existing programmes describing a
    segment that no longer exists. Retiring takes it out of the picker and
    leaves history readable. The count of programmes still on it is returned so
    the screen can say what was affected.
    """
    with SessionLocal() as s:
        tid = resolve_tenant_id(s, principal, mga)
        lid = _list_id(s, tid)
        if lid is None:
            raise HTTPException(404, "not found")
        row = s.execute(
            text("SELECT code FROM ref_code_value "
                 "WHERE value_id = :v AND list_id = :l"),
            {"v": value_id, "l": lid}).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        in_use = s.execute(
            text("SELECT count(*) FROM program "
                 "WHERE program_tenant_id = :t AND program_business_segment = :c"),
            {"t": tid, "c": row[0]}).scalar() or 0
        s.execute(text("UPDATE ref_code_value SET is_active = FALSE, "
                       "modified_at = :now WHERE value_id = :v"),
                  {"v": value_id, "now": datetime.utcnow()})
        s.commit()
        return {"ok": True, "name": row[0], "programmes_still_using": in_use}
