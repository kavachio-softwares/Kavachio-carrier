"""
db_persister.py
───────────────
Persist the contract-upload pipeline output (`final_output` from
ValidationRuleGenerator.generate_validation_rules_json) into the canonical
Postgres tables created in pgAdmin.

Single entry point:
  persist_pipeline_output(final_output, program_id, source_file) -> dict

Mapping (per db_tables_info.docx):
  program_metadata          → UPDATE program  +  program_field_extraction audit
  commercial_terms          → contract_term
  clauses_extracted         → clauses_extracted
  validation_rules          → validation_rule

A fresh `contract` row is inserted per upload to anchor the child-table FKs.
Everything runs inside ONE transaction on the canonical (Postgres) engine; any
error rolls the whole thing back so a failed upload leaves no partial rows.
"""

import json
import logging
import datetime
import os

from sqlalchemy import text

from db import canonical_engine

log = logging.getLogger(__name__)


# =========================================================
# Value mappings (enforce DB CHECK constraints)
# =========================================================

def _map_severity(value):
    """validation_rule.severity ∈ {critical, warning, info}."""
    v = (value or "").strip().lower()
    if v in ("critical", "crit"):
        return "critical"
    if v in ("major", "high", "warning", "warn"):
        return "warning"
    if v in ("minor", "low", "info", "informational"):
        return "info"
    return "warning"


def _map_stage(value):
    """validation_rule.validation_stage ∈ {input, output}."""
    v = (value or "").strip().lower()
    return v if v in ("input", "output") else "input"


def _map_clause_status(resolve_status_value, generated_rule_count, routed_to_review=False):
    """
    clauses_extracted.rule_generation_status ∈
      {pending, processing, rules_generated, not_rule_bearing, failed, in_review}.
    The pipeline's resolve_status emits pending/review/low_confidence/
    not_rule_bearing/error — map those onto the allowed set.

    A rule-bearing clause that produced NO rule but WAS routed to the review queue
    is 'in_review' (it was processed, just not mappable yet) — NOT 'pending',
    which would wrongly imply it hasn't been looked at.
    """
    v = (resolve_status_value or "").strip().lower()
    if generated_rule_count and generated_rule_count > 0:
        return "rules_generated"
    if v == "not_rule_bearing":
        return "not_rule_bearing"
    if v == "error":
        return "failed"
    if routed_to_review:
        return "in_review"
    return "pending"


def _engine_or_none(value):
    """clauses_extracted.classified_engine ∈ {ajv, custom} | NULL."""
    v = (value or "").strip().lower()
    return v if v in ("ajv", "custom") else None


def _clamp_conf(value):
    """NUMERIC(5,4) confidence columns must be within [0, 1]."""
    try:
        c = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, c))


def _jsonb(value):
    """Serialize a Python value for a :param::jsonb bind."""
    return json.dumps(value, default=str)


def _ir_compiled_sql(rule):
    """Return the deterministic compiled DuckDB SQL for an IR rule, else None.
    The SQL lives top-level (`compiled_sql`) and inside `rule_spec` (kind ir_v1).
    """
    sql = rule.get("compiled_sql")
    if sql:
        return sql
    spec = rule.get("rule_spec")
    if isinstance(spec, dict) and spec.get("kind") == "ir_v1":
        return spec.get("compiled_sql")
    return None


def _persist_ir_rule_sql(conn, rule_id, contract_id, template_id, rule):
    """Write a deterministic IR rule's compiled SQL into rule_sql (status='ok').

    Keyed by rule_id; rule_hash matches duckdb_validation.rule_hash so the
    runtime can recognise it. schema_hash is left NULL — IR SQL is compiled
    against stable Output-Template field names, so the runtime uses it for
    ir_v1 rules without a schema-hash match. Best-effort: never blocks the rule.
    """
    sql = _ir_compiled_sql(rule)
    if not sql or rule_id is None:
        return
    try:
        from duckdb_validation import rule_hash
        rh = rule_hash(rule)
    except Exception:
        rh = None

    # Print the exact compiled SQL going into rule_sql for this rule.
    print(
        f"\n[db_persister] rule_sql ← rule_id={rule_id} "
        f"({rule.get('rule_name') or 'Unnamed'}):\n{sql}\n"
    )

    try:
        conn.execute(
            text("""
                INSERT INTO rule_sql
                    (rule_id, contract_id, template_id, schema_hash, rule_hash,
                     sql_text, status, message, attempts)
                VALUES
                    (:rule_id, :contract_id, :template_id, NULL, :rule_hash,
                     :sql_text, 'ok', 'ir_v1 (deterministic compiler)', 0)
            """),
            {
                "rule_id": rule_id, "contract_id": contract_id,
                "template_id": template_id, "rule_hash": rh, "sql_text": sql,
            },
        )
    except Exception as exc:
        print(f"[db_persister] rule_sql insert skipped for rule {rule_id}: {exc}")


def _resolve_class_id(conn, name):
    """Look up rule_class_library.rule_class_id by class name (None if absent)."""
    if not name:
        return None
    res = conn.execute(
        text("SELECT rule_class_id FROM rule_class_library WHERE name = :n"),
        {"n": name},
    ).first()
    return res[0] if res else None


def persist_resolved_rules(
    *,
    contract_id,
    program_id,
    tenant_id,
    db_clause_id,
    output_template_id,
    validation_rules,
    actor="user",
    created_by=None,
):
    """Persist rules built by a human review-queue resolution for ONE existing
    clause (see manual_rule_resolution.generate_rules_for_clause_field).

    In a single transaction: insert each validation_rule (+ its compiled SQL),
    flip the clause's rule_generation_status to 'rules_generated' with the new
    rule count, and drop the clause's 'review' routing rows so it leaves the
    queue. Idempotent-friendly: callers pass freshly generated rules.

    `created_by` overrides the provenance stamped on each row and on the
    clause. It defaults to the manual-resolution form because that is who this
    was written for; the whole-contract path (rules generated for a template
    that only arrived later) passes its own, so a bulk regeneration is not
    recorded as somebody's hand-made fix.

    Returns: list of {rule_id, rule_name, output_field} for the inserted rows.
    """
    created_by = created_by or f"manual_resolution:{actor}"
    created = []
    with canonical_engine.begin() as conn:
        for r in validation_rules:
            if not isinstance(r, dict):
                continue
            ct = r.get("canonical_target") or {}
            new_rule_id = conn.execute(
                text("""
                    INSERT INTO validation_rule
                        (tenant_id, contract_id, program_id, rule_engine,
                         rule_class_id, rule_name, rule_description,
                         validation_stage, severity, canonical_target, rule_spec,
                         error_message, source_clause_id, source_verbatim_text,
                         source_page_number, generation_confidence, rule_status,
                         created_by)
                    VALUES
                        (:tenant_id, :contract_id, :program_id, :rule_engine,
                         :rule_class_id, :rule_name, :rule_description,
                         :validation_stage, :severity,
                         CAST(:canonical_target AS JSONB), CAST(:rule_spec AS JSONB),
                         :error_message, :source_clause_id, :source_verbatim_text,
                         :source_page_number, :generation_confidence, :rule_status,
                         :created_by)
                    RETURNING rule_id
                """),
                {
                    "tenant_id":        tenant_id,
                    "contract_id":      contract_id,
                    "program_id":       program_id,
                    "rule_engine":      (r.get("rule_engine") or "ir").lower(),
                    "rule_class_id":    _resolve_class_id(conn, r.get("rule_class")),
                    "rule_name":        r.get("rule_name") or "Unnamed rule",
                    "rule_description": r.get("rule_description") or "",
                    "validation_stage": _map_stage(r.get("validation_stage")),
                    "severity":         _map_severity(r.get("severity")),
                    "canonical_target": _jsonb(ct),
                    "rule_spec":        _jsonb(r.get("rule_spec") or {}),
                    "error_message":    r.get("error_message"),
                    "source_clause_id": db_clause_id,
                    "source_verbatim_text": r.get("source_verbatim_text"),
                    "source_page_number": r.get("source_page_number"),
                    "generation_confidence": _clamp_conf(r.get("generation_confidence")),
                    # Human-assigned the field, and it passed the verify gate →
                    # go live immediately (consistent with the IR generator default).
                    "rule_status":      r.get("rule_status") or "active",
                    "created_by":       created_by,
                },
            ).scalar()

            _persist_ir_rule_sql(conn, new_rule_id, contract_id,
                                 output_template_id, r)

            created.append({
                "rule_id":      new_rule_id,
                "rule_name":    r.get("rule_name"),
                "output_field": ct.get("output_field"),
                # How many of the template's sample rows this rule would flag, when
                # the verifier was able to measure it. A reviewer who has just bound
                # a clause to a column of their own choosing needs this: a rule that
                # flags most of the sample data is the signal that the column was
                # the wrong one, and nothing else on the screen would say so.
                "sample_impact": r.get("sample_impact"),
            })

        # `db_clause_id` is None for a rule that came from no clause of this
        # contract — a generic library rule, which the whole-contract path sends
        # through here with a NULL source. There is no clause to take out of the
        # review queue, and both statements below would match nothing anyway.
        if created and db_clause_id is not None:
            # The clause now has runnable rules → leave the review queue.
            conn.execute(
                text("""
                    UPDATE clauses_extracted
                       SET rule_generation_status = 'rules_generated',
                           generated_rule_count =
                               COALESCE(generated_rule_count, 0) + :n,
                           updated_at = now(),
                           updated_by = :actor
                     WHERE clause_id = :cid
                """),
                {"n": len(created), "actor": created_by,
                 "cid": db_clause_id},
            )
            conn.execute(
                text("""
                    DELETE FROM contract_clause_routing
                     WHERE contract_id = :cid AND clause_id = :clid
                       AND bucket = 'review'
                """),
                {"cid": contract_id, "clid": db_clause_id},
            )

    return created


def _parse_date(value, default):
    """Parse an ISO YYYY-MM-DD date; fall back to `default` on any failure."""
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return default


def _meta_value(field):
    """A program_metadata field is {value, source_text, page, confidence}."""
    if isinstance(field, dict):
        return field.get("value")
    return None


def _is_empty(value):
    return value in (None, "", [], {})


def _meta_scalar(field):
    """`_meta_value` coerced to the scalar a TEXT program column can hold.

    The extractor legitimately returns a list when one contract names several
    values for a single field — a retrocession contract covering GL1 and GL2
    yields product_line = ['GL1', 'GL2', 'Excess of Loss Retrocession'].
    psycopg2 adapts a Python list to a Postgres ARRAY, so the metadata UPDATE
    below fails with "COALESCE types text[] and text cannot be matched"
    against these String columns. Flatten to comma-joined text (a dict — a
    nested/structured value — to compact JSON) so the shape of the extracted
    value can never decide whether the upload persists. The untouched value is
    still kept verbatim in program_field_extraction.value (JSONB).
    """
    value = _meta_value(field)
    if isinstance(value, (list, tuple, set)):
        parts = [str(v).strip() for v in value if not _is_empty(v)]
        return ", ".join(p for p in parts if p) or None
    if isinstance(value, dict):
        return json.dumps(value, default=str, sort_keys=True)
    return value


# Upper bound on a stored upload token. The token is client-supplied, so it is
# bounded and trimmed before it is written; anything longer is not a token the
# client generated and is discarded rather than stored.
UPLOAD_TOKEN_MAX_LEN = int(os.getenv("KAVACHIO_UPLOAD_TOKEN_MAX_LEN", "128"))


def normalize_upload_token(token):
    """A storable upload token, or None.

    The caller that made the upload is the only one who knows which contract it
    is waiting for; this is the value that lets it say so. Anything that isn't a
    plain, bounded, non-empty string is dropped — a bad token must simply mean
    "no token" (the caller falls back to identifying its contract by programme +
    filename + template), never a failed upload.
    """
    if not isinstance(token, str):
        return None
    token = token.strip()
    if not token or len(token) > UPLOAD_TOKEN_MAX_LEN:
        return None
    return token


def build_extracted_payload(document_type, program_name, program_metadata,
                            upload_token=None, reference_documents=None,
                            identity=None, regen_report=None):
    """The contract row's `extracted` JSON.

    `upload_token` is the caller's correlation id for THIS upload. It rides in
    here rather than in a column of its own on purpose: `init_db()` skips all
    DDL under RLS, so a new column would exist on some deployments and not
    others, and the contract INSERT names its columns explicitly — a missing
    column would fail every contract upload. This object is already written on
    every insert and already returned by the contracts list, so it costs no
    schema change and cannot break the upload path.

    It is NESTED under "upload" rather than sitting at the top level, because
    `direct_routes._contract_constants` treats every top-level SCALAR in this
    object as a fixed value the contract supplies to the output, and those feed
    `constants_dict` on the run path. A bare `upload_token` string would quietly
    join that namespace; a dict is skipped by that scalar filter, so the token
    cannot leak into generated output.

    `reference_documents` is the same kind of nested object: {"external": [...],
    "provided": [...]} — the document(s) the contract defers rule content to but
    which were NOT supplied, plus the filenames that WERE. It is recorded here
    for the same reason the token is (no schema change, and this object is
    already read back by the setup screens), and nested for the same reason: the
    scalar filter in `direct_routes._contract_constants` skips non-scalars, so a
    reference document name can never leak into generated output as a constant.

    Both keys are only present when there is something to record, so rows
    written without them are byte-identical to before.
    """
    payload = {
        "document_type":    document_type,
        "program_name":     program_name,
        "program_metadata": program_metadata,
    }
    token = normalize_upload_token(upload_token)
    if token:
        payload["upload"] = {"token": token}
    refs = reference_documents or {}
    if refs.get("external") or refs.get("provided"):
        payload["reference_documents"] = {
            "external": refs.get("external") or [],
            "provided": refs.get("provided") or [],
        }
    # Regeneration stability (see regen_reconcile.py). Both are nested DICTS for
    # the same scalar-filter reason as `upload`: they must never leak into
    # `_contract_constants`. `identity` holds the L1/L2b hashes FUTURE uploads
    # match against; `regen_report` is the reconcile diff for THIS upload.
    if identity:
        payload["identity"] = dict(identity)
    if regen_report:
        payload["regen_report"] = regen_report
    return payload


def extracted_upload_token(extracted):
    """Read an upload token back out of a contract's `extracted` JSON, tolerating
    rows that predate it, rows where it is absent, and a column holding anything
    other than an object."""
    if not isinstance(extracted, dict):
        return None
    upload = extracted.get("upload")
    if not isinstance(upload, dict):
        return None
    return normalize_upload_token(upload.get("token"))


# =========================================================
# Public entry point
# =========================================================

def persist_pipeline_output(
    final_output,
    program_id,
    source_file,
    output_template_id=None,
    content_fingerprint=None,
    entity_fingerprint=None,
    upload_token=None,
    identity=None,
    prior_contract=None,
):
    """
    Persist `final_output` into the canonical Postgres tables.

    Returns a summary dict:
      {contract_id, program_id, tenant_id, counts: {...}}

    Raises ValueError if `program_id` is not found in the canonical program
    table (we never silently insert a program — its NOT NULL party FKs cannot
    be derived from extraction).

    `upload_token` is an optional correlation id supplied by the caller that
    made the upload, recorded so that caller can find THIS contract afterwards
    even if it never received the response (see build_extracted_payload).

    `identity` is regen_reconcile.identity_payload(...) — the L1/L2b hashes to
    stash on the contract row so future uploads can recognise this document.
    `prior_contract` is the upload route's identity-ladder hit
    ({contract_id, level, ...}) or None; when None, the L3 (business-identity)
    probe still runs here because it needs the extracted metadata. Either way a
    hit triggers reconcile-and-carry-forward over `validation_rules` BEFORE
    anything is inserted — see regen_reconcile.reconcile. Fail-open: any error
    in that path falls back to persisting the generated set unchanged.
    """

    program_metadata = final_output.get("program_metadata") or {}
    commercial_terms = final_output.get("commercial_terms") or []
    clauses          = final_output.get("clauses_extracted") or []
    validation_rules = final_output.get("validation_rules") or []
    # Clauses that did NOT become runnable rules. review = rule-bearing but not
    # mappable to the output template (needs data the BDX lacks); control =
    # non-rule-bearing (governance / obligations). Persisted so the UI can show
    # the FULL picture, not just the mapped rules.
    review_queue     = final_output.get("review_queue") or []
    control_register = final_output.get("control_register") or []
    meta             = final_output.get("metadata") or {}
    # External document(s) the contract defers to that were never supplied (the
    # user chose "Continue Without It", or the halt was off), plus the reference
    # files that WERE supplied. Recorded on the contract so the setup screens can
    # say WHY a deferred clause produced no rule — see build_extracted_payload.
    reference_documents = {
        "external": final_output.get("external_references") or [],
        "provided": final_output.get("reference_documents_provided") or [],
    }

    document_type = meta.get("document_type") or "unknown"

    today = datetime.date.today()
    # Validate program_id
    if not program_id or program_id <= 0:
        raise ValueError(f"Invalid program_id: {program_id}")

    log.info(f"[Persist] Received program_id: {program_id}")

    today = datetime.date.today()
    with canonical_engine.begin() as conn:

        # ── 1) Lookup program → tenant_id ────────────────────────────────
        row = conn.execute(
            text("SELECT program_tenant_id FROM program WHERE program_id = :pid"),
            {"pid": program_id}
        ).first()

        if row is None:
            raise ValueError(
                f"persist_pipeline_output: program_id {program_id} not found "
                f"in canonical 'program' table"
            )

        tenant_id = row[0]

        # ── 1b) Regeneration stability: reconcile against a prior version ─
        # The old decision is the default; this run only PROPOSES changes
        # (rule_status='needs_review' rows the engine never executes). See
        # regen_reconcile.py. KAVACHIO_CARRY_FORWARD=0 disables entirely.
        regen_report = None
        try:
            from contract_upload_services import regen_reconcile as _rr
            if _rr._enabled():
                prior = prior_contract
                if not prior:
                    prior = _rr.find_prior_contract_l3(
                        conn, tenant_id, program_metadata)
                if prior and prior.get("contract_id"):
                    prior_cid = prior["contract_id"]
                    # Same template ⇔ the prior row's content_fingerprint (doc
                    # text + template FIELD NAMES) equals this upload's. Only
                    # then can old compiled SQL be trusted on the new sheets.
                    prior_fp = conn.execute(
                        text("""SELECT row_hash FROM contract
                                WHERE contract_id = :cid"""),
                        {"cid": prior_cid},
                    ).scalar()
                    same_template = bool(
                        content_fingerprint and prior_fp == content_fingerprint)
                    # Column names of THIS template — lets the deterministic
                    # provenance gates police rules the normalizer never saw
                    # (carried-forward ones). See regen_reconcile.reconcile.
                    _field_names = (meta or {}).get("template_field_names") or []
                    old_rules = _rr.load_prior_rules(conn, prior_cid)
                    if old_rules:
                        validation_rules, regen_report = _rr.reconcile(
                            old_rules, validation_rules,
                            same_template=same_template,
                            field_names=_field_names)
                        # A check bound by an OLDER version but covered by
                        # nothing in the current set is re-surfaced as an inert
                        # proposal rather than lost for good.
                        if same_template:
                            _lin = [c["contract_id"] for c in
                                    _rr.find_contract_lineage(
                                        conn, tenant_id,
                                        content_fp=content_fingerprint,
                                        file_sha=(identity or {}).get("file_sha256"),
                                        doc_sha=(identity or {}).get("doc_sha256"))
                                    if c["contract_id"] != prior_cid]
                            if _lin:
                                _rec = _rr.recover_lost_coverage(
                                    conn, _lin, validation_rules, regen_report)
                                validation_rules = list(validation_rules) + _rec
                        regen_report["prior_contract_id"] = prior_cid
                        regen_report["match_level"] = prior.get("level")
                        log.info(
                            f"[Regen] prior contract {prior_cid} "
                            f"({prior.get('level')}, same_template="
                            f"{same_template}): carried "
                            f"{regen_report['carried']}, proposals "
                            f"{regen_report['params_changed'] + regen_report['column_changed'] + regen_report['only_in_new'] + regen_report['values_narrowed']}, "
                            f"not regenerated {regen_report['only_in_old']}, "
                            f"replaced-on-gate {regen_report.get('replaced_failing_gate', 0)}, "
                            f"paused-on-gate {regen_report.get('carried_paused_by_gate', 0)}, "
                            f"dupes {regen_report.get('duplicates_paused', 0)}, "
                            f"recovered {regen_report.get('recovered_from_lineage', 0)}")
                        try:
                            import pipeline_log as plog
                            plog.log("REGEN", "CARRY",
                                     f"prior contract {prior_cid} matched at "
                                     f"{prior.get('level')}",
                                     f"{regen_report['identical']} identical kept, "
                                     f"{regen_report['only_in_new']} new + "
                                     f"{regen_report['params_changed'] + regen_report['column_changed'] + regen_report['values_narrowed']} changed -> review, "
                                     f"{regen_report['only_in_old']} not regenerated (carried)")
                        except Exception:
                            pass
        except Exception as _regen_exc:  # noqa: BLE001 — fail-open by design
            print(f"[Regen] carry-forward skipped ({_regen_exc}) — "
                  f"persisting the generated set unchanged")
            regen_report = None

        # Behavioural duplicate guard — runs on EVERY upload, not just
        # regenerations. Two rules enforcing the same check on the same column
        # with the same values report every violation twice, and a first-time
        # setup can produce that pair just as easily as a regeneration can (the
        # model names rules freely, so only behaviour identifies them).
        try:
            from contract_upload_services import regen_reconcile as _rr2
            _dupe_report = regen_report if isinstance(regen_report, dict) else {"details": []}
            _rr2.dedupe_behavioural_twins(validation_rules, _dupe_report)
            _n = _dupe_report.get("duplicates_paused") or 0
            if _n:
                log.info(f"[Regen] {_n} duplicate rule(s) paused for review")
        except Exception as _dexc:  # noqa: BLE001 — fail-open
            print(f"[Regen] duplicate check skipped ({_dexc})")

        # ── 2) INSERT contract → contract_id ─────────────────────────────
        inception = _parse_date(_meta_scalar(program_metadata.get("inception_date")), today)
        expiry     = _parse_date(
            _meta_scalar(program_metadata.get("expiry_date")),
            inception.replace(year=inception.year + 1)
        )

        contract_name = source_file or f"contract_{program_id}"
        umr = f"UMR-{program_id}-{abs(hash(contract_name)) % 1_000_000:06d}"

        # ── Type-2 SCD versioning ────────────────────────────────────────
        # The contract table already carries the SCD columns. Close out the
        # prior current version(s) for this program before inserting the new
        # one, and stamp the new row as current with its content fingerprint.
        # The fingerprint is what lets an identical re-upload be reused
        # WITHOUT re-running the LLM (see contract_versioning.find_reusable_contract).
        conn.execute(
            text("""
                UPDATE contract
                SET    is_current_version = FALSE,
                       valid_until = now()
                WHERE  contract_program_id = :pid
                  AND  is_current_version IS TRUE
            """),
            {"pid": program_id},
        )

        # program/contract are now unified with the ops columns; the contract
        # screen reads the ops `filename` / `status_ops` / `extracted` columns,
        # so populate them too — not just the canonical contract_name.
        contract_id = conn.execute(
            text("""
                INSERT INTO contract
                    (tenant_id, contract_program_id, contract_primary_umr,
                     contract_name, contract_type,
                     contract_inception_date, contract_expiry_date,
                     filename, status_ops, extracted,
                     output_template_id, is_app_managed,
                     row_hash, row_key,
                     is_current_version, valid_from)
                VALUES
                    (:tenant_id, :program_id, :umr, :contract_name, :contract_type,
                     :inception_dt, :expiry_dt, :filename, :status_ops,
                     CAST(:extracted AS JSONB), :output_template_id, TRUE,
                     :content_fingerprint, :entity_fingerprint,
                     TRUE, now())
                RETURNING contract_id
            """),
            {
                "tenant_id":          tenant_id,
                "program_id":         program_id,
                "umr":                umr,
                "contract_name":      contract_name,
                "contract_type":      document_type,
                "inception_dt":       inception,
                "expiry_dt":          expiry,
                "filename":           source_file or contract_name,
                "status_ops":         "active",
                "extracted":          _jsonb(build_extracted_payload(
                    document_type,
                    _meta_scalar(program_metadata.get("program_name")),
                    program_metadata,
                    upload_token,
                    reference_documents,
                    identity=identity,
                    regen_report=regen_report,
                )),
                "output_template_id":  output_template_id,
                "content_fingerprint": content_fingerprint,
                "entity_fingerprint":  entity_fingerprint,
            }
        ).scalar_one()

        # ── 2b) Business-effective term (Feature 7 — Prior Period Files) ──
        # The SCD UPDATE above closed the prior version with `valid_until =
        # now()` — SYSTEM time, "we stopped believing this today". §7.1 asks a
        # different question: which version APPLIED on a transaction's date. So
        # this stamps the business-effective range and closes the predecessor at
        # the new version's effective_from, which is what makes a backdated
        # endorsement resolve correctly for dates already in the past.
        #
        # A separate UPDATE rather than extra columns on the INSERT above: the
        # INSERT stays byte-identical, so a deployment that has not applied
        # migration 20_1 keeps working exactly as before instead of failing on
        # an unknown column.
        #
        # Fail-open, and deliberately broad: effective dating is additive and
        # nothing reads it until CONTRACT_ASOF_ENABLED is on, so it must never
        # be the reason a contract upload fails.
        #
        # Wrapped in a SAVEPOINT, and that is load-bearing rather than tidy: in
        # PostgreSQL a failed statement aborts the whole transaction, so a
        # try/except alone would swallow the Python exception while leaving the
        # transaction dead — every statement after this point would then fail
        # with InFailedSqlTransaction, and the contract upload would break on
        # the one path this block was written to survive. The savepoint rolls
        # back only what happened inside it and hands the caller a working
        # transaction back.
        _sp = None
        try:
            from contract_upload_services import contract_asof as _asof
            _sp = conn.begin_nested()
            _eff_from = _parse_date(
                # Honoured if extraction ever surfaces an endorsement date; an
                # amendment's effective date is NOT its contract's inception,
                # and using inception for one would silently backdate the new
                # version over the whole original term.
                _meta_scalar(program_metadata.get("amendment_effective_date"))
                or _meta_scalar(program_metadata.get("endorsement_effective_date")),
                inception,
            )
            _lineage = _asof.lineage_of(conn, contract_id)
            if _lineage is not None:
                _asof.stamp_effective_dates(
                    conn, contract_id, _lineage, _eff_from, expiry,
                    version_label=_meta_scalar(
                        program_metadata.get("amendment_number")) or "Original",
                )
            if _sp is not None:
                _sp.commit()
        except Exception as _asof_exc:  # noqa: BLE001 — fail-open by design
            if _sp is not None:
                try:
                    _sp.rollback()
                except Exception:
                    pass
            print(f"[AsOf] effective dating skipped ({_asof_exc}) — "
                  f"contract {contract_id} persisted without a business term")

        # ── 3) UPDATE program metadata columns ───────────────────────────
        territory = _meta_scalar(program_metadata.get("territory"))

        program_name_val = _meta_scalar(program_metadata.get("program_name"))

        # Average confidence across populated metadata fields → program.extraction_confidence
        confs = [
            _clamp_conf(f.get("confidence"))
            for f in program_metadata.values()
            if isinstance(f, dict) and f.get("confidence") is not None
        ]
        confs = [c for c in confs if c is not None]
        avg_conf = round(sum(confs) / len(confs), 4) if confs else None

        conn.execute(
            text("""
                UPDATE program SET
                    program_name          = COALESCE(program_name, :program_name),
                    distribution_channel  = COALESCE(:distribution_channel, distribution_channel),
                    program_business_segment = COALESCE(:business_segment, program_business_segment),
                    program_product_line  = COALESCE(:product_line, program_product_line),
                    program_bordereau_frequency = COALESCE(:bdx_frequency, program_bordereau_frequency),
                    claims_basis          = COALESCE(:claims_basis, claims_basis),
                    extracted_from_contract_id = :contract_id,
                    extraction_confidence = :extraction_confidence,
                    modified_at           = now()
                WHERE program_id = :program_id
            """),
            {
                "program_name":         program_name_val,
                "distribution_channel": _meta_scalar(program_metadata.get("distribution_channel")),
                "business_segment":     _meta_scalar(program_metadata.get("business_segment")),
                "product_line":         _meta_scalar(program_metadata.get("product_line")),
                "bdx_frequency":        _meta_scalar(program_metadata.get("bdx_frequency")),
                "claims_basis":         _meta_scalar(program_metadata.get("claims_basis")),
                "contract_id":          contract_id,
                "extraction_confidence": avg_conf,
                "program_id":           program_id,
            }
        )

        # ── 4) UPSERT program_field_extraction (one row per field) ───────
        pfe_count = 0
        for field_name, field in program_metadata.items():

            if not isinstance(field, dict):
                continue

            value = field.get("value")
            if _is_empty(value):
                continue

            conn.execute(
                text("""
                    INSERT INTO program_field_extraction
                        (program_id, field_name, value, confidence, sources)
                    VALUES
                        (:program_id, :field_name, CAST(:value AS JSONB),
                         :confidence, CAST(:sources AS JSONB))
                    ON CONFLICT (program_id, field_name) DO UPDATE SET
                        value      = EXCLUDED.value,
                        confidence = EXCLUDED.confidence,
                        sources    = EXCLUDED.sources,
                        updated_at = now()
                """),
                {
                    "program_id": program_id,
                    "field_name": field_name,
                    "value":      _jsonb(value),
                    "confidence": _clamp_conf(field.get("confidence")) or 0.0,
                    "sources":    _jsonb([{
                        "source_text": field.get("source_text"),
                        "page":        field.get("page"),
                    }]),
                }
            )
            pfe_count += 1

        # ── 5) INSERT contract_term (one per commercial term) ────────────
        term_count = 0
        for term in commercial_terms:

            if not isinstance(term, dict):
                continue

            conn.execute(
                text("""
                    INSERT INTO contract_term
                        (tenant_id, term_contract_id, term_type, term_definition,
                         term_source_reference)
                    VALUES
                        (:tenant_id, :contract_id, :term_category,
                         CAST(:term_definition AS JSONB),
                         :clause_ref)
                """),
                {
                    "tenant_id":       tenant_id,
                    "contract_id":     contract_id,
                    "term_category":   term.get("term_type") or "other",
                    "term_definition": _jsonb(term.get("value")),
                    "clause_ref":      (term.get("source_text") or "")[:500] or None,
                }
            )
            term_count += 1

        # ── 6) INSERT clauses_extracted, build local→db clause-id map ────
        # Pre-count generated rules per local clause id so we can set
        # rule_generation_status / generated_rule_count correctly.
        rules_per_clause = {}
        for r in validation_rules:
            cid = r.get("source_clause_id")
            if cid is not None:
                rules_per_clause[cid] = rules_per_clause.get(cid, 0) + 1

        # Clauses that were rule-bearing but routed to the review queue (no rule
        # generated) → 'in_review', not 'pending'.
        review_clause_ids = {
            it.get("clause_id") for it in review_queue if isinstance(it, dict)
        }

        clause_id_map = {}   # local clause_id → db clause_id
        clause_count = 0

        for clause in clauses:

            if not isinstance(clause, dict):
                continue

            local_id = clause.get("clause_id")
            classification = clause.get("classification") or {}
            gen_count = rules_per_clause.get(local_id, 0)

            status = _map_clause_status(
                clause.get("rule_generation_status"), gen_count,
                routed_to_review=local_id in review_clause_ids,
            )

            db_clause_id = conn.execute(
                text("""
                    INSERT INTO clauses_extracted
                        (contract_id, clause_type, title, text, page_number,
                         section_header, extraction_confidence,
                         rule_generation_status, generated_rule_count,
                         classified_engine, classified_rule_types,
                         classification_confidence)
                    VALUES
                        (:contract_id, :clause_type, :title, :text, :page_number,
                         :section_header, :extraction_confidence,
                         :status, :gen_count,
                         :engine, CAST(:rule_types AS JSONB),
                         :class_conf)
                    RETURNING clause_id
                """),
                {
                    "contract_id":   contract_id,
                    "clause_type":   clause.get("clause_type") or "other",
                    "title":         clause.get("title"),
                    "text":          clause.get("text") or "",
                    "page_number":   clause.get("page_number") or clause.get("page"),
                    "section_header": clause.get("section_header"),
                    "extraction_confidence": _clamp_conf(clause.get("extraction_confidence")),
                    "status":        status,
                    "gen_count":     gen_count,
                    "engine":        _engine_or_none(classification.get("engine")),
                    "rule_types":    _jsonb(classification.get("rule_types") or []),
                    "class_conf":    _clamp_conf(classification.get("confidence")),
                }
            ).scalar_one()

            if local_id is not None:
                clause_id_map[local_id] = db_clause_id
            clause_count += 1

        # ── 6b) Persist clause hierarchy + intent (Root B) ───────────────
        # Best-effort: only when the migration adding parent_clause_id +
        # clause_intent has run. We probe information_schema FIRST (a SELECT) and
        # skip cleanly if absent — never let a failed UPDATE abort the surrounding
        # transaction on an un-migrated database.
        _hier_cols = {
            r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'clauses_extracted'")).fetchall()
        }
        if {"parent_clause_id", "clause_intent"} <= _hier_cols:
            for clause in clauses:
                if not isinstance(clause, dict):
                    continue
                db_id = clause_id_map.get(clause.get("clause_id"))
                if db_id is None:
                    continue
                parent_db = clause_id_map.get(clause.get("parent_clause_id"))
                intent = clause.get("clause_intent")
                if parent_db is None and not intent:
                    continue
                conn.execute(text(
                    "UPDATE clauses_extracted SET parent_clause_id = :p, "
                    "clause_intent = :i WHERE clause_id = :id"),
                    {"p": parent_db, "i": intent, "id": db_id})
        else:
            print("[persist] clauses_extracted.parent_clause_id/clause_intent "
                  "absent — skipping hierarchy persistence (run the migration).")

        # ── 7) INSERT validation_rule (resolve class id, remap clause id) ─
        rule_class_cache = {}

        def _resolve_class_id(name):
            if name in rule_class_cache:
                return rule_class_cache[name]
            res = conn.execute(
                text("SELECT rule_class_id FROM rule_class_library WHERE name = :n"),
                {"n": name}
            ).first()
            rid = res[0] if res else None
            rule_class_cache[name] = rid
            return rid

        rule_count = 0
        for r in validation_rules:

            if not isinstance(r, dict):
                continue

            local_clause = r.get("source_clause_id")
            db_clause = clause_id_map.get(local_clause)

            new_rule_id = conn.execute(
                text("""
                    INSERT INTO validation_rule
                        (tenant_id, contract_id, program_id, rule_engine,
                         rule_class_id, rule_name, rule_description,
                         validation_stage, severity, canonical_target, rule_spec,
                         error_message, source_clause_id, source_verbatim_text,
                         source_page_number, generation_confidence, rule_status,
                         created_by)
                    VALUES
                        (:tenant_id, :contract_id, :program_id, :rule_engine,
                         :rule_class_id, :rule_name, :rule_description,
                         :validation_stage, :severity,
                         CAST(:canonical_target AS JSONB), CAST(:rule_spec AS JSONB),
                         :error_message, :source_clause_id, :source_verbatim_text,
                         :source_page_number, :generation_confidence, :rule_status,
                         :created_by)
                    RETURNING rule_id
                """),
                {
                    "tenant_id":       tenant_id,
                    "contract_id":     contract_id,
                    "program_id":      program_id,
                    "rule_engine":     (r.get("rule_engine") or "ajv").lower(),
                    "rule_class_id":   _resolve_class_id(r.get("rule_class")),
                    "rule_name":       r.get("rule_name") or "Unnamed rule",
                    "rule_description": r.get("rule_description") or "",
                    "validation_stage": _map_stage(r.get("validation_stage")),
                    "severity":        _map_severity(r.get("severity")),
                    "canonical_target": _jsonb(r.get("canonical_target") or {}),
                    "rule_spec":       _jsonb(r.get("rule_spec") or {}),
                    "error_message":   r.get("error_message"),
                    "source_clause_id": db_clause,
                    "source_verbatim_text": r.get("source_verbatim_text"),
                    "source_page_number": r.get("source_page_number"),
                    "generation_confidence": _clamp_conf(r.get("generation_confidence")),
                    "rule_status":     r.get("rule_status") or "needs_review",
                    "created_by":      r.get("created_by") or "ai_generator_v1",
                }
            ).scalar()
            rule_count += 1

            # Deterministic IR rules already carry their compiled DuckDB SQL —
            # write it straight into rule_sql (status='ok') so the validation
            # runtime uses it directly and never calls the LLM to compile.
            _persist_ir_rule_sql(conn, new_rule_id, contract_id,
                                 output_template_id, r)

        # ── 8) Persist non-validatable clauses (review + control buckets) ─
        # Stored in a side table so the export-time DuckDB engine (which reads
        # validation_rule) never tries to run them, while the UI can still list
        # every clause with its routing + reason.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS contract_clause_routing (
                routing_id   SERIAL PRIMARY KEY,
                tenant_id    INTEGER,
                contract_id  INTEGER,
                program_id   INTEGER,
                clause_id    INTEGER,
                bucket       TEXT NOT NULL,
                rule_name    TEXT,
                clause_text  TEXT,
                source_page  INTEGER,
                reason       TEXT,
                created_at   TIMESTAMPTZ DEFAULT now()
            )
        """))
        # Replace prior routing rows for this contract (re-runs are idempotent).
        conn.execute(
            text("DELETE FROM contract_clause_routing WHERE contract_id = :cid"),
            {"cid": contract_id},
        )

        routing_count = 0
        for bucket, items in (("review", review_queue), ("control", control_register)):
            for it in items:
                if not isinstance(it, dict):
                    continue
                ir = it.get("ir") or {}
                conn.execute(
                    text("""
                        INSERT INTO contract_clause_routing
                            (tenant_id, contract_id, program_id, clause_id,
                             bucket, rule_name, clause_text, source_page, reason)
                        VALUES
                            (:tenant_id, :contract_id, :program_id, :clause_id,
                             :bucket, :rule_name, :clause_text, :source_page, :reason)
                    """),
                    {
                        "tenant_id":   tenant_id,
                        "contract_id": contract_id,
                        "program_id":  program_id,
                        "clause_id":   clause_id_map.get(it.get("clause_id")),
                        "bucket":      bucket,
                        "rule_name":   ir.get("rule_name") or it.get("rule_name"),
                        "clause_text": it.get("clause_text"),
                        "source_page": it.get("source_page"),
                        "reason":      it.get("reason"),
                    },
                )
                routing_count += 1

    summary = {
        "contract_id": contract_id,
        "program_id":  program_id,
        "tenant_id":   tenant_id,
        "counts": {
            "program_field_extraction": pfe_count,
            "contract_terms":           term_count,
            "clauses_extracted":        clause_count,
            "validation_rule":          rule_count,
            "review_queue":             len(review_queue),
            "control_register":         len(control_register),
            "clause_routing":           routing_count,
        },
    }

    print(
        f"[Persist] contract_id={contract_id} program_id={program_id} "
        f"tenant_id={tenant_id} → "
        f"pfe={pfe_count}, terms={term_count}, "
        f"clauses={clause_count}, rules={rule_count}, "
        f"routing={routing_count} (review={len(review_queue)}, "
        f"control={len(control_register)})"
    )

    return summary
