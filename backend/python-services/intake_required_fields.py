"""Feature 12.1 — does the file carry the columns this programme needs?

The gap this closes: a bordereau could open cleanly, have 40,000 rows, come
from a known broker, and still be the wrong export entirely — last year's
layout, a claims file where a premium file was expected, a report with none of
the columns the mapping reads. Every one of those was ACCEPTED, and only failed
an hour later when the run produced a sheet of blanks.

Where "required" comes from
---------------------------
Not from the contract PDF, and not from a model. The setup for this
(broker × programme) already records which input columns its mapping reads —
``DirectFormat.column_mapping`` is ``{output sheet: {output column: rule}}`` and
a copy rule names its ``source``. Those source columns ARE the required set, by
construction: they are precisely the columns whose absence produces blanks.

That makes this check plain SQL over rows that already exist. ``missing_columns``
answers a richer question (what does the CONTRACT require that the setup does
not even collect?) and pays a model call for it — right for setup time, far too
slow for something that has to answer in the first second of every file.

Column names are matched through ``field_aliases``, so a setup built on
"Commission" accepts a file whose header says "CM". Reporting a column missing
when it is sitting right there under a different name is the failure that module
exists to prevent, and it would be a bad first impression for this one.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from pathlib import Path
from typing import Optional

import field_aliases as fa
import intake_safety as safety

log = logging.getLogger("kavachio.intake.fields")

# Show a few names, not forty. A broker reading "Policy Number, Gross Premium
# and 38 others" goes and looks at their export; one reading forty column names
# closes the email.
_NAMES_SHOWN = 6


def _norm(name) -> str:
    return " ".join(str(name or "").lower().replace("_", " ").split())


# ── reading just the header row ─────────────────────────────────────────────

def read_headers(filename: str, file_bytes: bytes) -> list[str]:
    """Every column name in the file, across all sheets, deduplicated.

    Header-only on purpose. For xlsx this pulls one row per sheet out of the
    read-only stream rather than materialising the workbook; for CSV it reads
    the first line. The row COUNT still costs a full pass, but that already
    happened by the time this runs — this adds a header read, not a second scan.
    """
    ext = Path(filename).suffix.lower()
    seen: list[str] = []

    def _add(values) -> None:
        for v in values:
            text = str(v or "").strip()
            if text and text not in seen:
                seen.append(text)

    try:
        if ext in (".xlsx", ".xlsm"):
            from openpyxl import load_workbook
            wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
            try:
                for ws in wb.worksheets:
                    for row in ws.iter_rows(min_row=1, max_row=1, values_only=True):
                        _add(row or ())
                        break
            finally:
                wb.close()
        elif ext == ".xls":
            import pandas as pd
            for df in pd.read_excel(io.BytesIO(file_bytes), sheet_name=None,
                                    nrows=0).values():
                _add(df.columns)
        elif ext == ".csv":
            text = file_bytes.decode("utf-8-sig", errors="replace")
            first = next(csv.reader(io.StringIO(text)), [])
            _add(first)
        elif ext == ".json":
            payload = json.loads(file_bytes.decode("utf-8-sig"))
            if isinstance(payload, dict):
                for value in payload.values():
                    if isinstance(value, list):
                        payload = value
                        break
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                _add(payload[0].keys())
            elif isinstance(payload, dict):
                _add(payload.keys())
        elif ext == ".xml":
            root = safety.safe_xml_root(file_bytes)
            first = next(iter(root), None)
            if first is not None:
                _add(child.tag for child in first)
                _add(first.attrib.keys())
    except Exception as exc:
        # A file we cannot read the header of has already failed `can_open`, or
        # is a shape this check has nothing to say about. Never the reason a
        # file is refused.
        log.info("could not read headers from %s: %s", filename, exc)
        return []
    return seen


# ── what this programme's setup actually reads ──────────────────────────────

def expected_columns(session, route) -> list[str]:
    """The input columns the live setup's mapping copies from. Empty when there
    is no setup yet, which means this check has nothing to compare against and
    steps aside — the same way the live-contract check does."""
    if route is None:
        return []
    from db import DirectFormat, LandingRecord, Pipeline

    fmt = None
    try:
        q = (session.query(Pipeline)
             .filter(Pipeline.tenant_id == route.tenant_id,
                     Pipeline.status == "active"))
        program_id = getattr(route, "program_id", None)
        if program_id is not None:
            q = q.filter(Pipeline.program_id == program_id)
        broker_id = getattr(route, "broker_party_id", None)
        if broker_id is not None:
            # A pipeline built before the broker level existed has NULL here and
            # still serves every broker on the programme — so it must stay in
            # the running, not be filtered out.
            q = q.filter(Pipeline.broker_party_id.in_([broker_id, None]))
        pipe = q.order_by(Pipeline.id.desc()).first()
        if pipe and pipe.input_format_id:
            fmt = session.get(DirectFormat, pipe.input_format_id)

        if fmt is None and program_id is not None:
            # Pre-pipeline setups: the approved DirectFormat for the programme is
            # still what a run resolves against (see direct_routes' soak note).
            fmt = (session.query(DirectFormat)
                   .filter(DirectFormat.tenant_id == route.tenant_id,
                           DirectFormat.program_id == program_id,
                           DirectFormat.approved == 1)
                   .order_by(DirectFormat.id.desc()).first())
    except Exception as exc:
        log.info("could not resolve a setup for route %s: %s",
                 getattr(route, "id", "?"), exc)
        return []

    if fmt is None:
        return []

    wanted: list[str] = []
    mapping = fmt.column_mapping or {}
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except (ValueError, TypeError):
            mapping = {}
    for sheet_rules in (mapping or {}).values():
        if not isinstance(sheet_rules, dict):
            continue
        for rule in sheet_rules.values():
            if (isinstance(rule, dict) and rule.get("kind") == "copy"
                    and rule.get("source")):
                source = str(rule["source"]).strip()
                if source and source not in wanted:
                    wanted.append(source)

    if wanted:
        return wanted

    # No mapping saved yet (a setup mid-build). The columns the setup was
    # CAPTURED on are the next best statement of what the file should look
    # like — the same source the setup editor shows.
    try:
        rec = (session.query(LandingRecord)
               .filter(LandingRecord.format_id == fmt.id)
               .order_by(LandingRecord.id.desc()).first())
        for sheet in ((rec.data or {}).get("sheets") or {}).values() if rec else ():
            for col in (sheet.get("columns") or []):
                text = str(col or "").strip()
                if text and text not in wanted:
                    wanted.append(text)
    except Exception as exc:
        log.info("could not read captured columns for format %s: %s", fmt.id, exc)
    return wanted


# ── the check ───────────────────────────────────────────────────────────────

def _present(header_norms: set[str], headers: list[str], want: str) -> bool:
    if _norm(want) in header_norms:
        return True
    return any(fa.share_an_alias(want, have) for have in headers)


def check(session, route, filename: str, file_bytes: bytes) -> Optional[str]:
    """A refusal or a hold when the file does not carry what the setup reads.

    Two different failures, deliberately told apart:

      NOTHING matches  -> turned away. This is not a bordereau with a problem,
                          it is the wrong file, and there is nothing for a
                          person to decide.
      SOME missing     -> held. A layout that has genuinely changed is a
                          conversation, not a rejection — and it is exactly the
                          case where refusing automatically would be wrong.
    """
    wanted = expected_columns(session, route)
    if not wanted:
        return None                       # no setup to compare against

    headers = read_headers(filename, file_bytes)
    if not headers:
        return None                       # unreadable header — `can_open` owns that

    header_norms = {_norm(h) for h in headers}
    missing = [w for w in wanted if not _present(header_norms, headers, w)]
    if not missing:
        return None

    if len(missing) == len(wanted):
        return ("None of the columns this programme reports on are in this "
                "file. It looks like a different export — please check the "
                "file before sending it again.")

    shown = ", ".join(missing[:_NAMES_SHOWN])
    if len(missing) > _NAMES_SHOWN:
        shown += f" and {len(missing) - _NAMES_SHOWN} more"
    return (f"Held — the file is missing {len(missing)} column"
            f"{'' if len(missing) == 1 else 's'} this programme reports on: "
            f"{shown}.")
