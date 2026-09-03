"""One-shot migration of an existing Kavachio database to the v4 data model.

Order of work (mirrors docs/kavachio-datamodel/Kavachio_Data_Model_Old_vs_New.docx §7):
  1. ADD  — create the new tables and every missing v4 column (additive, reversible).
  2. COPY — copy each carried column's data into its renamed v4 successor
            (the old column is left in place; drop it later once verified).
  3. BACKFILL policyholder — one row per insured party, then point
            policy.policy_policyholder_id at it.
  4. REWRITE STRINGS — canonical-field names stored as plain strings
            (mappers.spec / spec_by_sheet / candidates,
            column_mapping_cache.canonical_field,
            export_templates.structure -> columns[].canonical_field)
            move to their v4 names via data_model.LEGACY_FIELD_MAP,
            in the same transaction as the copies.
  5. REPORT — every stored reference whose field was REMOVED from the model
            is listed (those mappings must be re-chosen by the mapper).

Idempotent: COPY only fills NULL targets; the string rewrite maps v4 names to
themselves. Run it once per environment, verify, then plan a separate pass to
drop the superseded columns.

Usage:
    DATABASE_URL=postgresql+psycopg2://... python scripts/migrate_to_v4_model.py [--dry-run]
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# main.py loads the .env; this script does not import main, so load it here or
# DATABASE_URL is unset and the engine points nowhere.
try:
    from pathlib import Path as _Path
    from dotenv import load_dotenv as _load
    _load(_Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

from sqlalchemy import inspect, text

DRY_RUN = "--dry-run" in sys.argv

# Old physical column -> v4 physical column, per carried table.
# Generated from Kavachio_Data_Model_Column_Mapping.docx.
RENAMES: dict[str, dict[str, str]] = {
  "app_user": {
    "broker_party_id": "user_broker_party_id",
    "email": "user_email",
    "full_name": "user_full_name",
    "role": "user_role",
    "status": "user_status"
  },
  "claim": {
    "catastrophe_code": "claim_catastrophe_code",
    "catastrophe_name": "claim_catastrophe_name",
    "closed_dt": "claim_closed_date",
    "content_fingerprint": "row_hash",
    "entity_fingerprint": "row_key",
    "first_reserve_dt": "claim_first_reserve_date",
    "ingest_batch_id": "upload_id",
    "loss_cause": "claim_loss_cause",
    "loss_desc": "claim_loss_description",
    "loss_dt": "claim_loss_date",
    "policy_id": "claim_policy_id",
    "reported_dt": "claim_reported_date"
  },
  "claim_reserve": {
    "as_of_date": "reserve_as_of_date",
    "claim_id": "reserve_claim_id",
    "ingest_batch_id": "upload_id"
  },
  "claim_transaction": {
    "claim_id": "claim_transaction_claim_id",
    "content_fingerprint": "row_hash",
    "currency_iso": "claim_transaction_currency",
    "entity_fingerprint": "row_key",
    "ingest_batch_id": "upload_id",
    "transaction_date": "claim_transaction_date",
    "transaction_type": "claim_transaction_type"
  },
  "contract": {
    "approval_status": "contract_approval_status",
    "broker_party_id": "contract_broker_party_id",
    "content_fingerprint": "row_hash",
    "entity_fingerprint": "row_key",
    "expiry_dt": "contract_expiry_date",
    "inception_dt": "contract_inception_date",
    "ingest_batch_id": "upload_id",
    "premium_cap_amount": "contract_premium_cap_amount",
    "program_id": "contract_program_id"
  },
  "contract_amendment": {
    "amendment_number": "contract_amendment_number",
    "changes": "contract_amendment_changes",
    "effective_date": "contract_amendment_effective_date",
    "executed_date": "contract_amendment_executed_date",
    "ingest_batch_id": "upload_id"
  },
  "contract_approval": {
    "acted_at": "approval_acted_at",
    "action": "approval_action",
    "contract_file_hash": "approval_file_hash",
    "contract_id": "approval_contract_id",
    "note": "approval_note"
  },
  "coverage": {
    "aggregate_limit": "coverage_aggregate_limit",
    "attachment_point": "coverage_attachment_point",
    "claims_basis": "coverage_claims_basis",
    "content_fingerprint": "row_hash",
    "deductible_amount": "coverage_deductible_amount",
    "entity_fingerprint": "row_key",
    "ingest_batch_id": "upload_id",
    "occurrence_limit": "coverage_occurrence_limit",
    "policy_id": "coverage_policy_id",
    "retro_date": "coverage_retro_date",
    "tiv": "coverage_total_insured_value"
  },
  "fx_rate": {
    "from_currency": "fx_rate_from_currency",
    "ingest_batch_id": "upload_id",
    "rate_source": "fx_rate_source",
    "to_currency": "fx_rate_to_currency"
  },
  "landing_correction": {
    "new_value": "correction_new_value",
    "old_value": "correction_old_value",
    "landing_id": "correction_landing_record_id"
  },
  "landing_record": {
    "delta_status": "landing_record_delta_status"
  },
  "party": {
    "content_fingerprint": "row_hash",
    "entity_fingerprint": "row_key",
    "ingest_batch_id": "upload_id",
    "is_active": "party_is_active",
    "legal_name": "party_legal_name",
    "tax_id": "party_tax_id"
  },
  "party_license": {
    "expiry_dt": "license_expiry_date",
    "ingest_batch_id": "upload_id",
    "party_id": "license_party_id"
  },
  "policy": {
    "cancel_reason": "policy_cancel_reason",
    "certificate_number": "policy_certificate_reference",
    "content_fingerprint": "row_hash",
    "contract_id": "policy_contract_id",
    "currency_iso": "policy_sum_insured_currency",
    "entity_fingerprint": "row_key",
    "ingest_batch_id": "upload_id",
    "insured_party_id": "policy_policyholder_id",
    "policy_effective_dt": "policy_effective_date",
    "policy_expiration_dt": "policy_expiration_date",
    "program_id": "policy_program_id"
  },
  "premium_transaction": {
    "booking_dt": "premium_transaction_booking_date",
    "content_fingerprint": "row_hash",
    "coverage_id": "premium_transaction_coverage_id",
    "entity_fingerprint": "row_key",
    "fx_rate_date": "premium_transaction_fx_rate_date",
    "ingest_batch_id": "upload_id",
    "original_currency": "premium_transaction_original_currency",
    "policy_id": "premium_transaction_policy_id",
    "transaction_effective_dt": "premium_transaction_effective_date",
    "transaction_expiry_dt": "premium_transaction_expiry_date",
    "transaction_type": "premium_transaction_type"
  },
  "program": {
    "annual_statement_lob": "program_annual_statement_line_of_business",
    "bdx_frequency": "program_bordereau_frequency",
    "business_segment": "program_business_segment",
    "content_fingerprint": "row_hash",
    "due_after_days": "program_due_after_days",
    "entity_fingerprint": "row_key",
    "ingest_batch_id": "upload_id",
    "product_line": "program_product_line"
  },
  "program_broker": {
    "broker_party_id": "program_broker_party_id",
    "status": "program_broker_status"
  },
  "ref_code_list": {
    "ingest_batch_id": "upload_id",
    "is_active": "code_list_is_active",
    "list_name": "code_list_name",
    "list_version": "code_list_version"
  },
  "ref_code_value": {
    "ingest_batch_id": "upload_id",
    "is_active": "code_value_is_active",
    "list_id": "code_value_list_id"
  },
  "reinsurance_arrangement": {
    "content_fingerprint": "row_hash",
    "entity_fingerprint": "row_key",
    "ingest_batch_id": "upload_id"
  },
  "rule_class_library": {
    "applies_to": "rule_class_applies_to",
    "description": "rule_class_description",
    "execution_mode": "rule_class_execution_mode",
    "ingest_batch_id": "upload_id",
    "required_lookups": "rule_class_required_lookups",
    "sql_template": "rule_class_sql_template"
  },
  "rule_sql": {
    "attempts": "rule_sql_attempts",
    "contract_id": "rule_sql_contract_id",
    "message": "rule_sql_message",
    "rule_hash": "rule_sql_rule_hash",
    "schema_hash": "rule_sql_schema_hash",
    "sql_text": "rule_sql_text",
    "status": "rule_sql_status",
    "template_id": "rule_sql_template_id"
  },
  "tenant": {
    "ingest_batch_id": "upload_id",
    "is_active": "tenant_is_active",
    "legal_name": "tenant_legal_name"
  },
  "upload": {
    "bdx_type": "upload_bordereau_type",
    "file_hash": "upload_file_hash",
    "filename": "upload_filename",
    "load_type_detected": "upload_load_type_detected",
    "num_rows": "upload_rows_total",
    "period_end": "upload_period_end",
    "period_start": "upload_period_start"
  },
  "validation_exception": {
    "ingest_batch_id": "upload_id",
    "rule_id": "exception_rule_id",
    "status": "exception_status",
    "validation_run_id": "exception_run_id"
  },
  "validation_rule": {
    "class_name": "rule_class_name",
    "contract_id": "rule_contract_id",
    "field_path": "rule_field_path",
    "ingest_batch_id": "upload_id",
    "params": "rule_params",
    "severity": "rule_severity"
  },
  "validation_run": {
    "ingest_batch_id": "upload_id",
    "rule_set_version": "run_rule_set_version",
    "status": "run_status"
  }
}

# Columns folded ACROSS tables (old table.column -> new table.column).
FOLDS = [
    # party_address (one address per party) -> party columns
    ("party_address", "address_line1", "party", "party_address_line1", "party_id", "party_id"),
    ("party_address", "city",          "party", "party_city",          "party_id", "party_id"),
    ("party_address", "state_code",    "party", "party_subdivision",   "party_id", "party_id"),
    ("party_address", "zip_code",      "party", "party_postal_code",   "party_id", "party_id"),
    ("party_address", "country",       "party", "party_country",       "party_id", "party_id"),
]


def _has(insp, table, column=None):
    try:
        cols = {c["name"] for c in insp.get_columns(table)}
    except Exception:
        return False
    return True if column is None else column in cols


def step1_add(engine):
    """Create v4 tables and ALTER in every missing canonical column."""
    from canonical import canonical_metadata, CANONICAL_TABLES
    print("== 1. ADD ==")
    if not DRY_RUN:
        canonical_metadata.create_all(engine)
    with engine.begin() as conn:
        insp = inspect(conn)
        for t_name, t in CANONICAL_TABLES.items():
            if not insp.has_table(t_name):
                continue
            existing = {c["name"] for c in insp.get_columns(t_name)}
            for col in t.c:
                if col.name in existing:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                print(f"  ALTER TABLE {t_name} ADD COLUMN {col.name} {ddl}")
                if not DRY_RUN:
                    conn.exec_driver_sql(
                        f'ALTER TABLE {t_name} ADD COLUMN {col.name} {ddl}')


def _orm_renames() -> dict[str, dict[str, str]]:
    """Renames derived from the ORM mapping itself.

    db.py keeps STABLE Python attribute names over the renamed v4 physical
    columns — `tenant_name = Column("tenant_code", ...)`. Wherever the
    attribute name differs from the column name, the attribute name IS the old
    physical column, so the pair is a rename.

    This is authoritative in a way the column-mapping doc is not: the doc misses
    pairs it classed as "removed" or "unchanged" (tenant.tenant_name ->
    tenant_code, app_user.tenant_id -> user_tenant_id), and those two alone
    leave nobody able to log in. Merged UNDER the doc's map, so the doc still
    wins where both describe the same column.
    """
    import db
    from sqlalchemy import inspect as _sains
    out: dict[str, dict[str, str]] = {}
    # Column.key mirrors the column NAME, not the attribute — the attribute
    # binding lives on the mapper, so read it from there.
    for mapper in db.Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        for attr, prop in _sains(mapper.class_).mapper.column_attrs.items():
            col = prop.columns[0]
            if col.table is not table or attr == col.name:
                continue
            out.setdefault(table.name, {})[attr] = col.name
    return out


def _kind(sa_type) -> str:
    """Coarse type family, so a copy is only attempted between compatible ones."""
    t = str(sa_type).upper()
    if any(k in t for k in ("INT", "SERIAL", "NUMERIC", "DECIMAL", "REAL",
                            "DOUBLE", "FLOAT")):
        return "number"
    if "BOOL" in t:
        return "bool"
    if "TIMESTAMP" in t or "DATETIME" in t or t.startswith("DATE") or "TIME" in t:
        return "time"
    if "JSON" in t:
        return "json"
    return "text"


def _compatible(src_type, dst_type) -> bool:
    """True when `src` can be assigned to `dst` without a cast.

    Postgres refuses text → integer outright, and a silent cast would be worse
    than the refusal: `ingest_batch_id` is a batch LABEL while v4's `upload_id`
    is a foreign key to upload.upload_id. The column-mapping doc lists them as
    a rename because both are lineage, but they are different types AND
    different meanings, so the copy must be skipped rather than coerced.
    """
    s_k, d_k = _kind(src_type), _kind(dst_type)
    if s_k == d_k:
        return True
    # widening that Postgres accepts implicitly
    return (s_k, d_k) in {("number", "text"), ("bool", "text"), ("time", "text")}


def step2_copy(engine):
    """Copy carried columns into their renamed successors (NULL targets only).

    Each statement runs in its OWN transaction. One incompatible pair must not
    roll back the other 150 copies — that is exactly what happened the first
    time this ran against a real database.
    """
    print("== 2. COPY renamed columns ==")
    skipped: list[str] = []
    applied = 0

    # The doc's map, plus every rename the ORM implies (see _orm_renames).
    renames: dict[str, dict[str, str]] = {}
    for src in (_orm_renames(), RENAMES):
        for tbl, cols in src.items():
            renames.setdefault(tbl, {}).update(cols)

    with engine.connect() as conn:
        insp = inspect(conn)
        plans: list[tuple[str, str]] = []          # (label, sql)
        for table, cols in renames.items():
            if not insp.has_table(table):
                continue
            types = {c["name"]: c["type"] for c in insp.get_columns(table)}
            for old, new in cols.items():
                if old not in types or new not in types or old == new:
                    continue
                if not _compatible(types[old], types[new]):
                    skipped.append(f"{table}.{old} ({types[old]}) -> "
                                   f"{new} ({types[new]}): incompatible types")
                    continue
                plans.append((f"{table}.{old} -> {new}",
                              f"UPDATE {table} SET {new} = {old} "
                              f"WHERE {new} IS NULL AND {old} IS NOT NULL"))
        for (ot, oc, nt, nc, ok, nk) in FOLDS:
            if not (insp.has_table(ot) and insp.has_table(nt)):
                continue
            if not (_has(insp, ot, oc) and _has(insp, nt, nc)):
                continue
            src_t = {c["name"]: c["type"] for c in insp.get_columns(ot)}[oc]
            dst_t = {c["name"]: c["type"] for c in insp.get_columns(nt)}[nc]
            if not _compatible(src_t, dst_t):
                skipped.append(f"{ot}.{oc} -> {nt}.{nc}: incompatible types")
                continue
            plans.append((f"{ot}.{oc} -> {nt}.{nc}",
                          f"UPDATE {nt} SET {nc} = src.{oc} FROM {ot} src "
                          f"WHERE src.{ok} = {nt}.{nk} AND {nt}.{nc} IS NULL "
                          f"AND src.{oc} IS NOT NULL"))

    for label, sql in plans:
        print(" ", sql)
        if DRY_RUN:
            continue
        try:
            with engine.begin() as conn:          # one transaction per statement
                conn.exec_driver_sql(sql)
            applied += 1
        except Exception as exc:                  # noqa: BLE001
            skipped.append(f"{label}: {type(exc).__name__}: "
                           f"{str(exc).splitlines()[0][:120]}")

    if skipped:
        print("\n  -- NOT copied (left for a human to decide) --")
        for sk in skipped:
            print(f"     {sk}")
    if not DRY_RUN:
        print(f"\n  copied {applied} column(s); skipped {len(skipped)}")


def step3_policyholder(engine):
    """One policyholder per insured party; point the policy at it.

    The old model keyed the insured on the policy number (one company insured
    three times = three party rows); rows dedupe onto policyholder_natural_key
    = tenant :: casefolded legal name [:: tax id]."""
    print("== 3. BACKFILL policyholder ==")
    with engine.begin() as conn:
        insp = inspect(conn)
        needed = (insp.has_table("policyholder")
                  and _has(insp, "policy", "insured_party_id")
                  and _has(insp, "policy", "policy_policyholder_id"))
        if not needed:
            print("  (nothing to do — insured_party_id absent or policyholder missing)")
            return
        sql_insert = """
            INSERT INTO policyholder
                (policyholder_tenant_id, policyholder_natural_key,
                 policyholder_legal_name, policyholder_tax_id, created_at, modified_at)
            SELECT DISTINCT ON (p.tenant_id, lower(trim(p.legal_name)))
                   p.tenant_id,
                   p.tenant_id || '::' || lower(trim(p.legal_name)),
                   p.legal_name, p.tax_id, now(), now()
            FROM party p
            WHERE p.party_type = 'insured'
              AND COALESCE(trim(p.legal_name), '') <> ''
              AND NOT EXISTS (
                    SELECT 1 FROM policyholder ph
                    WHERE ph.policyholder_tenant_id = p.tenant_id
                      AND ph.policyholder_natural_key =
                          p.tenant_id || '::' || lower(trim(p.legal_name)))
        """
        sql_point = """
            UPDATE policy po
            SET    policy_policyholder_id = ph.policyholder_id
            FROM   party pa
            JOIN   policyholder ph
              ON   ph.policyholder_tenant_id = pa.tenant_id
             AND   ph.policyholder_natural_key =
                   pa.tenant_id || '::' || lower(trim(pa.legal_name))
            WHERE  po.insured_party_id = pa.party_id
              AND  po.policy_policyholder_id IS NULL
        """
        # The old party table used `tenant_id`/`legal_name`/`tax_id`; if step 2
        # already renamed them, read the new names instead.
        if not _has(insp, "party", "legal_name"):
            sql_insert = (sql_insert.replace("p.legal_name", "p.party_legal_name")
                                    .replace("p.tax_id", "p.party_tax_id")
                                    .replace("p.tenant_id", "p.party_tenant_id"))
            sql_point = (sql_point.replace("pa.legal_name", "pa.party_legal_name")
                                  .replace("pa.tenant_id", "pa.party_tenant_id"))
        for sql in (sql_insert, sql_point):
            print(" ", " ".join(sql.split())[:120], "…")
            if not DRY_RUN:
                conn.exec_driver_sql(sql)


def _map_field(field, legacy_map, current):
    if field in current:
        return field, False
    return legacy_map.get(field), True


def step4_rewrite_strings(engine):
    """Move stored canonical-field strings to their v4 names."""
    from data_model import DATA_MODEL, LEGACY_FIELD_MAP
    current = set(DATA_MODEL)
    dropped: dict[str, int] = {}
    print("== 4. REWRITE stored canonical-field strings ==")

    def remap_spec(spec):
        """{canonical: source} -> renamed dict; returns (new, changed)."""
        if not isinstance(spec, dict):
            return spec, False
        out, changed = {}, False
        for k, v in spec.items():
            if isinstance(v, dict):           # nested {sheet: {canonical: src}}
                nv, ch = remap_spec(v)
                out[k] = nv
                changed = changed or ch
                continue
            if k.startswith("_xf:") or k in current:
                out[k] = v
                continue
            nk = LEGACY_FIELD_MAP.get(k)
            if nk:
                out[nk] = v
                changed = True
            else:
                dropped[k] = dropped.get(k, 0) + 1
                changed = True                # dropped from the spec
        return out, changed

    with engine.begin() as conn:
        insp = inspect(conn)

        # mappers.spec / spec_by_sheet / candidates
        if insp.has_table("mappers"):
            rows = conn.execute(text(
                "SELECT id, spec, spec_by_sheet, candidates FROM mappers")).fetchall()
            for rid, spec, sbs, cands in rows:
                updates = {}
                for colname, blob in (("spec", spec), ("spec_by_sheet", sbs)):
                    if isinstance(blob, str):
                        try:
                            blob = json.loads(blob)
                        except Exception:
                            continue
                    nv, ch = remap_spec(blob) if blob else (blob, False)
                    if ch:
                        updates[colname] = json.dumps(nv)
                if isinstance(cands, str):
                    try:
                        cands = json.loads(cands)
                    except Exception:
                        cands = None
                if isinstance(cands, dict):
                    ch_any = False
                    for src, lst in cands.items():
                        if not isinstance(lst, list):
                            continue
                        kept = []
                        for cand in lst:
                            if not isinstance(cand, dict):
                                kept.append(cand); continue
                            cf = cand.get("canonical")
                            nf, was_legacy = _map_field(cf, LEGACY_FIELD_MAP, current)
                            if nf is None:
                                dropped[cf] = dropped.get(cf, 0) + 1
                                ch_any = True
                                continue
                            if was_legacy and nf != cf:
                                cand = {**cand, "canonical": nf}
                                ch_any = True
                            kept.append(cand)
                        cands[src] = kept
                    if ch_any:
                        updates["candidates"] = json.dumps(cands)
                if updates:
                    sets = ", ".join(f"{c} = :{c}" for c in updates)
                    print(f"  mappers id={rid}: rewrite {list(updates)}")
                    if not DRY_RUN:
                        conn.execute(text(
                            f"UPDATE mappers SET {sets} WHERE id = :id"),
                            {**updates, "id": rid})

        # column_mapping_cache.canonical_field
        if insp.has_table("column_mapping_cache"):
            rows = conn.execute(text(
                "SELECT id, canonical_field FROM column_mapping_cache")).fetchall()
            for rid, cf in rows:
                nf, was_legacy = _map_field(cf, LEGACY_FIELD_MAP, current)
                if not was_legacy:
                    continue
                if nf is None:
                    dropped[cf] = dropped.get(cf, 0) + 1
                    print(f"  column_mapping_cache id={rid}: DELETE ({cf} removed)")
                    if not DRY_RUN:
                        conn.execute(text(
                            "DELETE FROM column_mapping_cache WHERE id = :id"),
                            {"id": rid})
                elif nf != cf:
                    print(f"  column_mapping_cache id={rid}: {cf} -> {nf}")
                    if not DRY_RUN:
                        conn.execute(text(
                            "UPDATE column_mapping_cache SET canonical_field = :nf "
                            "WHERE id = :id"), {"nf": nf, "id": rid})

        # export_templates.structure -> sheets[].columns[].canonical_field
        if insp.has_table("export_templates"):
            rows = conn.execute(text(
                "SELECT id, structure FROM export_templates "
                "WHERE structure IS NOT NULL")).fetchall()
            for rid, struct in rows:
                if isinstance(struct, str):
                    try:
                        struct = json.loads(struct)
                    except Exception:
                        continue
                if not isinstance(struct, dict):
                    continue
                changed = False
                for sh in struct.get("sheets") or []:
                    for col in sh.get("columns") or []:
                        cf = col.get("canonical_field")
                        if not cf or cf.startswith("_xf:"):
                            continue
                        nf, was_legacy = _map_field(cf, LEGACY_FIELD_MAP, current)
                        if not was_legacy:
                            continue
                        if nf is None:
                            dropped[cf] = dropped.get(cf, 0) + 1
                            col["canonical_field"] = None   # unmapped -> re-choose
                        else:
                            col["canonical_field"] = nf
                        changed = True
                if changed:
                    print(f"  export_templates id={rid}: structure rewritten")
                    if not DRY_RUN:
                        conn.execute(text(
                            "UPDATE export_templates SET structure = :s "
                            "WHERE id = :id"),
                            {"s": json.dumps(struct), "id": rid})

    if dropped:
        print("== 5. REMOVED fields still referenced (mapper must re-choose) ==")
        for k, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5d}x {k}")
    else:
        print("== 5. No removed-field references found ==")


def main():
    from db import engine
    print(f"target: {engine.url.render_as_string(hide_password=True)}"
          + ("  [DRY RUN]" if DRY_RUN else ""))
    step1_add(engine)
    step2_copy(engine)
    step3_policyholder(engine)
    step4_rewrite_strings(engine)
    print("done.")


if __name__ == "__main__":
    main()
