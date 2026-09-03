"""Choosing an output template's columns from BOTH sides — scenarios Y to AK.

The plan asks for a template built from a reporting standard; the prompt changes
add that a standard's published list is per-territory and long, and that a good
deal of it does not apply to a given binder. So the field list has to be decided
from the standard AND the contract AND what the incoming bordereau actually
carries, and this is what proves it does.

REAL DATA THROUGHOUT. The output side is the bundled Lloyd's v5.2 workbook read
structurally — no field names are written down here. The input side is the
column list of a bordereau a carrier really loaded onto this platform, taken
from its stored mapping. Nothing here reaches the network or the database: the
model rung is exercised by handing ``cross_reference`` the proposal a model
would have returned, which is the point anyway — the model proposes and this
code decides (plan section 12).

    python test_output_source_analysis.py
"""
from __future__ import annotations

import output_source_analysis as osa
import output_template_fields as otf
import reporting_standards as standards

# The columns of a real bordereau loaded against Carrier 1 / Programme A. Short,
# operational names — which is exactly why a published standard's long
# descriptive headings do not match them by name alone.
INPUT_COLUMNS = [
    'APD  Unit Limit',
    'APD Auditable (Y/N)',
    'APD Deductible',
    'APD Premium',
    'APD per Occ / Terminal Limit',
    'AccountID',
    'Aggregate Deductible APD',
    'Aggregate Deductible MTC',
    'Attachment Point',
    'Basket Deductible',
    'BrokBranch',
    'Broker',
    'BtotalMileage',
    'BtotalPUnits',
    'BtotalRev',
    'BtotalTIV',
    'BtotalTrailers',
    'Cancel Reason',
    'Carrier',
    'Carrier Net/Net Premium',
    'Commission Amount',
    'Commission Rate',
    'Cumulative Prem',
    'Division',
    'EKVCmated Annual Premium',
    'Endt No.',
    'Endt detail',
    'Excess of',
    'ExpiringPolicyNumber',
    'Fac Coverage',
    'Fac Net Premium',
    'Faculative Re(Y/N)',
    'Gross Premium',
    'Highest Value Unit',
    'IPRM APD',
    'IPRM MTC',
    'Inception Prem',
    'Insured',
    'Insured City',
    'Insured County',
    'Insured State',
    'Insured Street',
    'Insured Zip Code',
    'Invoice #',
    'LA SL Broker Phone No.',
    'MTC AUDITABLE (Y/N)',
    'MTC Deductible',
    'MTC Premium',
    'MTC per Vehicle Limit',
    'MTC per occurrence',
    'NJ Transaction No',
    'Net Premium',
    'New/Renewal',
    'Participating Layer/TIV',
    'Perils Covered',
    'Perils Covered APD',
    'Perils Covered MTC',
    'Pol Occ Limit',
    'Policy No',
    'PolicyExpiration',
    'PolicyInception',
    'Primary/Excess',
    'QS Reinsurers Participation%',
    'Quote Share or Excess',
    'Reins Eff Date',
    'Reins Exp Date',
    'Reinsurance Limit',
    'Reinsurer',
    'Reinsurer Cert %',
    'Reporter Y/N',
    'Risk State',
    'State of Filing',
    'Surplus Lines Broker Address',
    'Surplus Lines Broker City',
    'Surplus Lines Broker License',
    'Surplus Lines Broker State',
    'Surplus Lines Broker Zip',
    'Surplus Lines Filing Broker',
    'THBBdxDate',
    'TRIA PREM',
    'Terminal Limit Scheduled',
    'Terminal Limit UnScheduled',
    'Transaction Type',
    'TransactionDate',
    'TransactionEffectiveDate',
    'TransactionExpirationDate',
    'UW',
]

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def library(jurisdiction: str) -> list[dict]:
    """The territory's published field list, straight out of the workbook."""
    std = standards.discover()[0]
    return [{"field": f["field"], "required": f["required"],
             "data_type": "string", "origin": "standard"}
            for f in standards.fields(std["id"], jurisdiction)]


def analysed(jurisdiction: str, *, cols=INPUT_COLUMNS, extra=None,
             samples=None, semantic=None):
    """One analysis pass with the model rung switched off (deterministic)."""
    fields = library(jurisdiction) + list(extra or [])
    rows = osa.cross_reference(fields, list(cols), samples or {}, use_model=False)
    if semantic:
        # Stand in for the model: re-answer the named fields with a proposal in
        # hand, exactly as the second pass does with a real one.
        import semantic_mapping as sm
        for i, r in enumerate(rows):
            if r["field"] in semantic:
                rows[i] = osa._verdict(
                    dict(r), sm.resolve_field(osa._out_field(r), list(cols),
                                              samples or {},
                                              semantic=semantic[r["field"]]))
    osa.recommend(rows, checked_input=bool(cols))
    return rows


def by_name(rows, name):
    return next(r for r in rows if r["field"] == name)


# --- Y. a territory's list is not taken wholesale ---------------------------
rows = analysed("Singapore Risk")
counts = osa.summarise(rows, checked_input=True)
check("Y. the published territory list is longer than what this binder reports",
      counts["dropped"] > 0 and counts["recommended"] < counts["total"],
      f"{counts['recommended']} kept of {counts['total']}")

# --- Z. every mandatory field survives, matched or not ----------------------
missed = [r["field"] for r in rows if r["required"] and not r["recommended"]]
check("Z. a field the standard makes mandatory is never dropped",
      not missed, f"{sum(1 for r in rows if r['required'])} mandatory, {len(missed)} dropped")

# --- AA. an optional field the data carries is kept, and says so ------------
# Picked structurally — the territory's first non-mandatory field — so the test
# never depends on which field that happens to be.
OPTIONAL = next(r["field"] for r in rows if not r["required"])
check("AA0. it is dropped while nothing in the bordereau matches it",
      not by_name(rows, OPTIONAL)["recommended"],
      f"{OPTIONAL}: {by_name(rows, OPTIONAL)['recommend_reason']}")
matched = analysed("Singapore Risk",
                   semantic={OPTIONAL: {"source": INPUT_COLUMNS[0],
                                        "confidence": 1.0}})
row = by_name(matched, OPTIONAL)
check("AA. the same optional field is kept once the bordereau carries it",
      row["recommended"] and row["in_input"], row["recommend_reason"])
check("AA2. and the reason names the column it came from",
      "bordereau" in row["recommend_reason"]
      and INPUT_COLUMNS[0] in row["recommend_reason"])

# --- AB. what the contract asks for is kept whatever the data holds ---------
CONTRACT_ONLY = {"field": "Retro Cession Reference", "required": True,
                 "data_type": "string", "origin": "contract_ai",
                 "reason": "named in the reporting clause"}
rows_c = analysed("Singapore Risk", extra=[CONTRACT_ONLY])
row = by_name(rows_c, CONTRACT_ONLY["field"])
check("AB. a field only the contract names is kept",
      row["recommended"] and not row["in_input"], row["recommend_reason"])
check("AB2. and the reason says it is the contract's doing",
      "contract" in row["recommend_reason"])

# --- AC. the model proposes, the ladder still decides -----------------------
# A real proposal from a real run: Lloyd's asks for the insured's full name, the
# bordereau column is simply "Insured".
INSURED = next(r["field"] for r in rows
               if r["field"].lower().startswith("insured full name"))
strong = analysed("Singapore Risk",
                  semantic={INSURED: {"source": "Insured", "confidence": 1.0}})
weak = analysed("Singapore Risk",
                semantic={INSURED: {"source": "Insured", "confidence": 0.72}})
check("AC. a 100% proposal that passes the checks is accepted",
      by_name(strong, INSURED)["in_input"] and
      by_name(strong, INSURED)["input_column"] == "Insured")
check("AC2. the same proposal at 72% is not auto-accepted",
      not by_name(weak, INSURED)["in_input"],
      by_name(weak, INSURED)["mapping_status"] or "")
check("AC3. but 72% is still enough to keep the column in the template",
      by_name(weak, INSURED)["recommended"],
      by_name(weak, INSURED)["recommend_reason"])

# --- AD. a proposal that fails the data check is refused --------------------
# The prompt's own example: the name fits, the values do not.
NUMERIC = next((r["field"] for r in rows if "premium" in r["field"].lower()), None)
mismatch = analysed(
    "Singapore Risk",
    cols=INPUT_COLUMNS + ["Prem Basis"],
    samples={"Prem Basis": ["Commercial", "Commercial", "Personal", "Commercial"]},
    extra=[{"field": "Premium Amount This Time", "required": False,
            "data_type": "decimal", "origin": "standard"}],
    semantic={"Premium Amount This Time":
              {"source": "Prem Basis", "confidence": 1.0}})
row = by_name(mismatch, "Premium Amount This Time")
check("AD. a perfect-confidence proposal is refused when the values disagree",
      not row["in_input"], row["mapping_reason"])

# --- AE. no input file = nothing is dropped on the data's say-so ------------
blind = analysed("Singapore Risk", cols=[])
blind_counts = osa.summarise(blind, checked_input=False)
check("AE. with no bordereau to check, only the standard decides",
      blind_counts["matched_input"] == 0 and
      blind_counts["recommended"] == sum(1 for r in blind if r["required"]),
      f"{blind_counts['recommended']} kept, all of them mandatory")
check("AE2. and a dropped field says the bordereau was never seen",
      all("upload the input template" in r["recommend_reason"]
          for r in blind if not r["recommended"]))

# --- AF. applying the selection switches columns off, never deletes them ----
structure = {"sheets": [{"sheet_name": "BDX", "columns": [
    {"column_index": i, "column_name": r["field"], "samples": []}
    for i, r in enumerate(matched)]}]}
otf.complete_structure(structure)
for col in structure["sheets"][0]["columns"]:
    col["system_required"] = by_name(matched, col["column_name"])["required"]
osa.apply_selection(
    structure, osa.keep_set([r for r in matched if r["recommended"]]),
    {r["field"].lower(): r for r in matched})
cols_now = structure["sheets"][0]["columns"]
check("AF. nothing is deleted — the sample stays aligned",
      len(cols_now) == len(matched))
check("AF2. what the user dropped is switched off",
      sum(1 for c in cols_now if not c.get("active", True))
      == osa.summarise(matched, checked_input=True)["dropped"])
check("AF3. a mandatory column cannot be switched off by a selection",
      all(c.get("active", True) for c in cols_now if c.get("system_required")))
check("AF4. the matched input column is recorded on the template",
      any(c.get("input_match", {}).get("column") for c in cols_now))

# --- AG. the selection reaches the DELIVERED FILE, in every format ----------
# Switching a column off is only meaningful if the file that goes out does not
# carry it. The same structure is run through the real writers, with one column
# also renamed, so the two rules are checked together: a dropped column is
# absent, and a kept one appears under its NEW heading while its values are
# still read from the original key.
import output_serializers as ser

sheet = structure["sheets"][0]
kept_col = next(c for c in sheet["columns"] if c.get("active", True))
gone_col = next(c for c in sheet["columns"] if not c.get("active", True))
kept_col["display_name"] = kept_col["column_name"] + " (Renamed)"

record = {c["column_name"]: f"v-{i}" for i, c in enumerate(sheet["columns"])}
neutral = ser.sheets_from_blocks(
    structure, [{"sheet": sheet["sheet_name"], "records": [record]}])
heads, keys = neutral[0]["headers"], neutral[0]["columns"]

check("AG. the dropped column is not in the file",
      gone_col["column_name"] not in heads and gone_col["column_name"] not in keys,
      gone_col["column_name"])
check("AG2. the kept column appears under its new heading",
      kept_col["display_name"] in heads and kept_col["column_name"] not in heads)
check("AG3. and its values are still read from the original key",
      kept_col["column_name"] in keys)

# xlsx is written by the style-preserving path, not by serialize().
for fmt in [f for f in ser.SUPPORTED_FORMATS if f != "xlsx"]:
    text = ser.serialize(neutral, fmt).decode("utf-8", "replace")
    check(f"AG4/{fmt}. new heading in, dropped column out, value carried",
          kept_col["display_name"] in text
          and gone_col["column_name"] not in text
          and record[kept_col["column_name"]] in text)

# --- AH. a column the contract asks for and the standard never publishes ----
# The territory's list is not a superset of the binder's obligations. A field
# the contract names and the published list has no column for used to be shown
# and then dropped, which shipped a file quietly missing something the contract
# requires. It is now appended after the published columns — and appended
# OPTIONAL, because only the standard can make a column mandatory.
CONTRACT_EXTRAS = [
    {"field": "Administrator Fee - Inspection", "required": True,
     "data_type": "string", "origin": "contract",
     "contract_reference": "clause 7.2 — inspection fee is reported separately"},
    {"field": "Facultative Reinsurance Placement", "required": False,
     "data_type": "string", "origin": "contract_rule"},
]

published = library("US")
published_names = {f["field"].strip().lower() for f in published}
check("AH. the extras are genuinely absent from the published list",
      not any(f["field"].strip().lower() in published_names for f in CONTRACT_EXTRAS))

both = analysed("US", extra=CONTRACT_EXTRAS)
kept = [r for r in both if r["recommended"]]
struct = {"sheets": [{"sheet_name": "BDX", "columns": [
    {"column_index": i, "column_name": f["field"], "samples": []}
    for i, f in enumerate(published)]}]}
otf.complete_structure(struct)
otf.apply_standard(struct, standards.fields(standards.discover()[0]["id"], "US"))
added = osa.append_contract_fields(struct, kept)
osa.apply_selection(struct, osa.keep_set(kept),
                    {r["field"].lower(): r for r in kept})

live = otf.active_columns(struct["sheets"][0])
names = [c["column_name"] for c in live]
check("AH2. every contract column the user kept is in the template",
      added == len(CONTRACT_EXTRAS)
      and all(f["field"] in names for f in CONTRACT_EXTRAS),
      f"{added} appended")
check("AH3. they are appended AFTER the published columns",
      names[-len(CONTRACT_EXTRAS):] == [f["field"] for f in CONTRACT_EXTRAS])
extras_now = [c for c in live if c.get("from_contract")]
check("AH4. and they go in optional, whatever the contract said",
      len(extras_now) == len(CONTRACT_EXTRAS)
      and not any(c.get("required") or c.get("system_required") for c in extras_now))
check("AH5. the contract's own wording is kept, so the editor can say why",
      any(c.get("contract_note") for c in extras_now))
check("AH6. a published column is not duplicated by an extra of the same name",
      osa.append_contract_fields(struct, [
          {"field": published[0]["field"], "origin": "contract"}]) == 0)
check("AH7. the appended columns reach the delivered file",
      all(f["field"] in ser.sheets_from_blocks(
              struct, [{"sheet": "BDX", "records": [{}]}])[0]["headers"]
          for f in CONTRACT_EXTRAS))

# --- AI. the contract's wording folded onto the column that already exists ---
# The contract says "commission %"; the standard publishes "Commission %". Two
# names, one column — and a merge keyed on the exact string makes two of them,
# so the file goes out with a twin beside every field the contract worded its
# own way. The deterministic rungs are enough for this pair, so the model is
# off and the answer is repeatable.
FROM_CONTRACT = [
    {"field": "commission %", "required": True, "origin": "contract"},
    {"field": "Inspection Fee", "required": False, "origin": "contract"},
]
lib = library("US")
extras, folded = osa.fold_contract_fields(lib, FROM_CONTRACT, use_model=False)
check("AI. a contract field the standard already publishes is not added twice",
      "commission %" in folded, f"folded onto {list(folded)}")
check("AI2. and one it does not publish is kept as a new column",
      [e["field"] for e in extras] == ["Inspection Fee"])
check("AI3. the fold is recorded against the published column, not lost",
      folded["commission %"]["required"] is True)

# The published row then carries the contract's ask, so it is kept even when
# the standard itself calls the column optional and no data matches it.
optional_name = next(f["field"] for f in lib if not f["required"])
rows = [dict(f, origin="standard") for f in lib]
for r in rows:
    if r["field"] == optional_name:
        r["also_in_contract"] = True
        r["contract_required"] = True
osa.recommend(rows, checked_input=True)
folded_row = by_name(rows, optional_name)
check("AI4. a published column the contract asks for is kept, not dropped",
      folded_row["recommended"]
      and "contract requires it too" in folded_row["recommend_reason"],
      optional_name)

# --- AJ. the unresolved report reads the template, not the proposal ---------
# An appended contract column goes in OPTIONAL, so it must not turn up in the
# "required with nothing to fill it" report the template screen shows. The
# report is read off the columns after the selection, which is the only place
# that knows what the file will actually say.
demand = [dict(f, required=True, origin="contract") for f in CONTRACT_EXTRAS]
struct2 = {"sheets": [{"sheet_name": "BDX", "columns": [
    {"column_index": i, "column_name": f["field"], "samples": []}
    for i, f in enumerate(published)]}]}
otf.complete_structure(struct2)
otf.apply_standard(struct2, standards.fields(standards.discover()[0]["id"], "US"))
kept2 = [r for r in analysed("US", extra=demand) if r["recommended"]]
osa.append_contract_fields(struct2, kept2)
osa.apply_selection(struct2, osa.keep_set(kept2),
                    {r["field"].lower(): r for r in kept2})
report = struct2.get("source_check", {}).get("unresolved_required", [])
check("AJ. an appended contract column is not reported as unresolved-required",
      not any(f["field"] in report for f in CONTRACT_EXTRAS),
      f"{len(report)} reported")
check("AJ2. a mandatory published column with nothing to fill it still is",
      all(any(c["column_name"] == n and (c.get("required") or c.get("system_required"))
              for c in struct2["sheets"][0]["columns"]) for n in report))

# --- AK. the two ways of building a template must not converge --------------
# "Reporting standard" and "Contract-based" are offered as a choice, so they
# have to produce different files. They stopped doing that when the contract
# route merged in the WHOLE published list: both then came out as the territory
# list with a few extras, and the choice was decoration. The standard's part in
# a contract-built template is the handful of columns every bordereau carries.
std_id = standards.discover()[0]["id"]
full = standards.fields(std_id, "US")
essential = standards.fields(std_id, "US", scope="essential")
check("AK. the standard's own layout is the whole published list",
      len(full) == 134, f"{len(full)} fields")
check("AK2. what a contract template borrows is far smaller",
      0 < len(essential) < len(full) / 4, f"{len(essential)} fields")
check("AK3. and it is exactly what the standard calls mandatory",
      {f["field"] for f in essential} == {f["field"] for f in full if f["required"]})
check("AK4. it carries the columns a contract never names",
      {"Coverholder Name", "Reporting Period (End Date)", "Original Currency"}
      <= {f["field"] for f in essential})
check("AK5. asking for the full list is still the default",
      [f["field"] for f in standards.fields(std_id, "US")] == [f["field"] for f in full])

CONTRACT_ONLY = [
    {"field": "Inspection Fee", "required": True, "origin": "contract"},
    {"field": "Claim Reference", "required": True, "origin": "contract"},
]
as_std = osa.recommend(osa.cross_reference(
    [dict(f, origin="standard") for f in full] + CONTRACT_ONLY,
    list(INPUT_COLUMNS), {}, use_model=False), checked_input=True)
as_ctr = osa.recommend(osa.cross_reference(
    [dict(f, origin="standard") for f in essential] + CONTRACT_ONLY,
    list(INPUT_COLUMNS), {}, use_model=False), checked_input=True)
check("AK6. the two routes now propose visibly different lists",
      len(as_std) > len(as_ctr) * 3, f"{len(as_std)} vs {len(as_ctr)}")
check("AK7. and the contract's own terms are in both",
      all(any(r["field"] == f["field"] for r in rows) for f in CONTRACT_ONLY
          for rows in (as_std, as_ctr)))

print()
print("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}")
raise SystemExit(1 if FAILURES else 0)
