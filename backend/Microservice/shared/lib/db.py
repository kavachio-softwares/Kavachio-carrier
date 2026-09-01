"""Persistence layer.

Everything lives in a single PostgreSQL database (`kavachio`):
  - Ops/bookkeeping → mappers, uploads, parties, programs, …
  - Canonical warehouse → the 53 xlsx-derived data-model tables

NOTE: the ops ORM classes below (Upload, Party, Program, Contract, PartyContact,
AppUser) map onto the SAME singular physical tables as the canonical schema in
canonical.py — they are two views of one set of rows. Tenancy is keyed on
`tenant_id` (FK → the canonical `tenant` table); the old `mga` string
discriminator and the `tenant_configs` table have been removed (see
docs/db_repair_notes.md). `tenant_name` on the `tenant` table holds the legacy
mga code, so the API still accepts an `mga` string and resolves it to a
tenant_id via `_get_tenant_id` / `_ensure_tenant`.

Env override:
  DATABASE_URL  postgresql+psycopg2://postgres:postgres123@192.168.2.11:5432/kavachio_carrier

(`LOCAL_DB_URL` / `CANONICAL_DB_URL` are still honoured for backward
compatibility — if set, they override DATABASE_URL.)
"""
import json
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, JSON, DateTime, Date, ForeignKey, Text,
    LargeBinary, Boolean, inspect, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from canonical import canonical_metadata  # 53 canonical xlsx-derived tables

# Single source of truth. Legacy env names are still accepted so existing
# deployments / scripts keep working; they all point at the same `kavachio` DB.
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("CANONICAL_DB_URL")
    or os.getenv("LOCAL_DB_URL")
)

# ── Optional Row-Level-Security mode (OFF by default — see docs/db_repair_notes.md) ──
# When KAVACHIO_RLS is truthy the app connects as the non-superuser `kavachio_app`
# role (so the DB's RLS policies actually apply) and stamps `app.tenant_id` on every
# transaction from a per-request context var. OFF -> connect as today (superuser),
# no SET LOCAL, create_all() still runs: i.e. nothing changes until explicitly enabled.
# Activation also requires migrations 07/08 applied + a per-request tenant set via
# set_current_tenant() (the auth phase wires that centrally).
import contextvars

RLS_ENABLED = os.getenv("KAVACHIO_RLS", "").strip().lower() in ("1", "true", "on", "yes")
# Connection string for the non-superuser app role (kavachio_app). Required when RLS_ENABLED.
APP_DB_URL = os.getenv("KAVACHIO_APP_DB_URL")

# Per-request tenant for RLS. Set this (e.g. from a dependency / auth) BEFORE opening
# the session whose queries must be tenant-scoped. None -> session sees only global
# (tenant_id IS NULL) rows under RLS (fail-closed).
_current_tenant_id: "contextvars.ContextVar[int | None]" = contextvars.ContextVar(
    "kavachio_current_tenant_id", default=None)


def set_current_tenant(tenant_id):
    """Set the tenant_id RLS will scope subsequent sessions to (no-op effect when RLS off)."""
    _current_tenant_id.set(int(tenant_id) if tenant_id is not None else None)


def get_current_tenant():
    return _current_tenant_id.get()


_effective_url = APP_DB_URL if (RLS_ENABLED and APP_DB_URL) else DATABASE_URL

# One engine, one database. The canonical_* / *Local aliases below are kept so
# the ~100 existing call sites (SessionLocal(), CanonicalSession()) keep working
# unchanged — they now all share this single connection.
engine = create_engine(_effective_url, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)

canonical_engine = engine          # alias — same single database
CanonicalSession = SessionLocal    # alias — same session factory

if RLS_ENABLED:
    # Stamp app.tenant_id on each transaction so RLS policies can match it.
    from sqlalchemy import event as _event

    @_event.listens_for(SessionLocal, "after_begin")
    def _apply_tenant_scope(session, transaction, connection):  # noqa: ANN001
        tid = _current_tenant_id.get()
        if tid is not None:
            # SET LOCAL = transaction-scoped, so it cannot leak across pooled requests.
            connection.exec_driver_sql("SET LOCAL app.tenant_id = %s", (str(tid),))
        # tid is None -> leave app.tenant_id unset -> current_setting(...,true) is NULL
        #             -> policy matches only tenant_id IS NULL rows (fail-closed).

# Back-compat aliases for any module / script importing these names.
LOCAL_DB_URL = DATABASE_URL
CANONICAL_DB_URL = DATABASE_URL

Base = declarative_base()


class Mapper(Base):
    __tablename__ = "mappers"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id (enforced in DB)
    # Template versioning. A "template" is the set of mapper rows that share
    # (mga, name); each row is one version. `is_active` marks the single
    # version actually used to read a matching BDX upload. Legacy rows have
    # name=NULL and fall back to signature-based grouping.
    name = Column(String, index=True, nullable=True)
    version = Column(Integer, default=1)
    is_active = Column(Integer, default=0)
    carrier = Column(String, index=True, nullable=True)
    contract = Column(String, index=True, nullable=True)
    party_id = Column(Integer, nullable=True, index=True)
    # signature = sorted list of source columns; used to detect format match
    signature = Column(JSON, nullable=False)
    # Flat legacy spec: {canonical_field: source_column}
    spec = Column(JSON, nullable=False)
    # NEW: per-sheet spec {sheet_name: {canonical_field: source_or_list}}
    # consumed by mapper.apply_spec_multi
    spec_by_sheet = Column(JSON, nullable=True)
    # Top-N ranked candidates per source column, used by the UI to render a
    # confidence-ordered picker:
    #   { "Sheet :: Column": [{canonical, confidence, reason}, ...] }
    candidates = Column(JSON, nullable=True)
    # Up-to-5 example values per source column, same keys as `candidates`:
    #   { "Sheet :: Column": ["val1", "val2", ...] }
    # Surfaced in the "Map input format" review UI as the Sample column.
    samples = Column(JSON, nullable=True)
    # Tenant's own "output column" per source column, derived from the tenant
    # Setup (DirectFormat.column_mapping copy rules), same keys as `candidates`:
    #   { "Sheet :: Column": "TenantOutputFieldName" }
    # Surfaced in the "Map input format" review UI as the Output column.
    output_by_source = Column(JSON, nullable=True)
    approved = Column(Integer, default=0)
    # Original BDX workbook bytes + filename + sheet selection. Lets the UI
    # offer "download" and "view sheet selection" after the fact.
    source_filename = Column(String, nullable=True)
    source_blob = Column(LargeBinary, nullable=True)
    # Blob-storage pointer (Azure/Azurite). When set, the bytes live in blob
    # storage and source_blob is NULL; falls back to source_blob otherwise.
    source_blob_ref = Column(String, nullable=True)
    selected_sheets = Column(JSON, nullable=True)
    # Optional pointer into kavachio.column_mapping_fingerprint — the
    # canonical cross-tenant home for the spec + similarity search vector.
    # Operational fields (blob, draft flag, etc.) stay on this row;
    # canonical_mapping / hit_count / SCD versioning live on the fingerprint.
    fingerprint_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Upload(Base):
    """One row per /bdx/upload call. Use its id to fetch the rows it produced."""
    __tablename__ = "upload"
    id = Column("upload_id", Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False)
    party_id = Column(Integer, nullable=True, index=True)
    mapper_id = Column(Integer, ForeignKey("mappers.id"), nullable=True)
    source_file = Column(String)
    sheets = Column(JSON)               # list of sheet names parsed
    counts_by_sheet = Column(JSON)      # {sheet: row_count}
    total_rows = Column(Integer, default=0)
    source_blob = Column(LargeBinary, nullable=True)
    # Blob-storage pointer (Azure/Azurite); see Mapper.source_blob_ref.
    source_blob_ref = Column(String, nullable=True)
    ingested_at = Column(DateTime, default=datetime.utcnow)


class BDXRecord(Base):
    """Legacy raw-payload row store. Kept for backward compatibility but the
    canonical relational tables (canonical.py) are now the source of truth."""
    __tablename__ = "dwh_bdx"
    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, ForeignKey("upload.upload_id"), index=True, nullable=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id (enforced in DB)
    mapper_id = Column(Integer, ForeignKey("mappers.id"))
    source_file = Column(String)
    sheet_name = Column(String, index=True, nullable=True)
    payload = Column(JSON)
    ingested_at = Column(DateTime, default=datetime.utcnow)


class ExportTemplate(Base):
    """User-defined output BDX template.

    `structure` captures the parsed shape of the sample workbook:
        {
          "sheets": [
            {
              "sheet_name": str,
              "header_row": int,           # 0-indexed
              "data_start_row": int,       # 0-indexed
              "columns": [
                {
                  "column_index": int,
                  "column_name": str,
                  "samples": [str, ...],
                  "canonical_field": str | None,  # data_model key
                  "transform": str | None,
                  "static_value": str | None
                }
              ],
              "row_strategy": str          # which entity each row represents
            }
          ]
        }
    """
    __tablename__ = "export_templates"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id (enforced in DB)
    name = Column(String, nullable=False)
    # Template versioning — rows sharing (mga, name) are versions of one
    # output template; `is_active` marks the version used to generate output.
    version = Column(Integer, default=1)
    is_active = Column(Integer, default=0)
    carrier = Column(String, nullable=True)
    # Link to the carrier party (party.party_id) and the specific contract
    # this template's output should be validated against. One carrier (party)
    # can have multiple contracts; templates must be bound to a contract to
    # be usable for validation/generation.
    carrier_party_id = Column(Integer, nullable=True)
    contract_id = Column(Integer, nullable=True)
    structure = Column(JSON, nullable=False)
    # Original sample workbook bytes — used to preserve fonts, colors, borders,
    # column widths, merged cells, etc. when generating output.
    template_blob = Column(LargeBinary, nullable=True)
    # Blob-storage pointer (Azure/Azurite); see Contract.blob_ref.
    template_blob_ref = Column(String, nullable=True)
    approved = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class OutputExport(Base):
    """One generated output file (a "download").

    Stores the rendered bytes so the user can re-download or view the data of a
    previously generated file, plus any validation exceptions found while
    checking the output. Contract-rule validation is a work in progress — for
    now `exceptions` holds generic completeness checks.
    """
    __tablename__ = "output_exports"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id (enforced in DB)
    template_id = Column(Integer, nullable=True)
    template_name = Column(String, nullable=True)
    filename = Column(String, nullable=False)
    source_upload_id = Column(Integer, nullable=True)
    policy_ids = Column(JSON, nullable=True)          # canonical policy_ids exported
    generated_by = Column(String, nullable=True)      # actor email
    policy_count = Column(Integer, default=0)
    exception_count = Column(Integer, default=0)
    exceptions = Column(JSON, nullable=True)          # [{severity, code, sheet, row, field, message}]
    status = Column(String, default="clean")          # clean | has_exceptions
    blob = Column(LargeBinary, nullable=True)         # the generated xlsx
    # Blob-storage pointer (Azure/Azurite); when set, the xlsx lives in blob
    # storage and `blob` is NULL. Falls back to `blob` otherwise.
    blob_ref = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class UploadPolicy(Base):
    """Mapping (upload, policy) so we can find every canonical row produced
    by a given /bdx/upload call."""
    __tablename__ = "upload_policy"
    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, ForeignKey("upload.upload_id"), index=True, nullable=False)
    policy_id = Column(Integer, index=True, nullable=False)  # canonical policy.policy_id


# --- App-facing CRUD entities backing the wireframes -----------------------

class Tenant(Base):
    """Canonical tenant row (S-02 settings live here now). Replaces the old
    `tenant_configs` table, which has been folded into `tenant` and dropped.

    `tenant_name` holds the legacy mga code, so resolving an inbound `mga`
    string is `SELECT tenant_id FROM tenant WHERE tenant_name = :mga`.
    Mapped read/write surface only — the canonical SCD2 columns (valid_from,
    content_fingerprint, …) exist in the DB but aren't needed by the app CRUD.
    """
    __tablename__ = "tenant"
    id = Column("tenant_id", Integer, primary_key=True)
    tenant_name = Column(String, unique=True, index=True, nullable=False)  # legacy mga code
    legal_name = Column(String, nullable=True)
    tenant_type = Column(String, nullable=True)  # party_type_e enum in DB
    address = Column(JSON, nullable=True)
    currency = Column(String, nullable=True)      # ISO 4217 (USD, EUR, GBP …)
    internal_codes = Column(JSON, nullable=True)  # JSONB free-form
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Party(Base):
    """Counterparty / insured / carrier directory entry (S-03 / S-03a)."""
    __tablename__ = "party"
    id = Column("party_id", Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True)
    # True = created via the app (the directory shows these); False = BDX-ingested
    # into the canonical data model. Replaces the old "mga IS NOT NULL" marker.
    is_app_managed = Column(Boolean, default=True)
    scope = Column(String, default="tenant")
    party_type = Column(String, nullable=False)
    legal_name = Column(String, nullable=False, index=True)
    dba_name = Column(String, nullable=True)
    tax_id = Column(String, nullable=True)
    naics_code = Column(String, nullable=True)
    am_best_rating = Column(String, nullable=True)
    domicile_country = Column(String, nullable=True)
    primary_jurisdiction = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    addresses = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PartyContact(Base):
    """Tenant-specific contact persons at a party."""
    __tablename__ = "party_contact"
    id = Column("contact_id", Integer, primary_key=True)
    party_id = Column(Integer, ForeignKey("party.party_id"), index=True, nullable=False)
    tenant_id = Column(Integer, index=True, nullable=True)
    full_name = Column(String, nullable=True)
    title = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Program(Base):
    """Program (S-05). Metadata can be AI-extracted from a contract."""
    __tablename__ = "program"
    id = Column("program_id", Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True)
    is_app_managed = Column(Boolean, default=True)  # see Party.is_app_managed
    party_id = Column(Integer, ForeignKey("party.party_id"), nullable=True, index=True)
    name = Column("program_name", String, nullable=False)
    lead_carrier = Column(String, nullable=True)
    admin_party = Column(String, nullable=True)
    bdx_frequency = Column(String, nullable=True)
    business_segment = Column(String, nullable=True)
    product_line = Column(String, nullable=True)
    distribution_channel = Column(String, nullable=True)
    territory = Column(String, nullable=True)
    commercial_terms = Column(JSON, nullable=True)
    status = Column("status_ops", String, default="draft")
    source_contract_file = Column(String, nullable=True)
    # canonical_program_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Contract(Base):
    """Contract file linked to a program and an Output Template.

    output_template_id links this contract to the output template whose field
    names were the mapping target for LLM rule extraction.
    Hierarchy: Contract Fields → Output Template Fields → Data Model Fields.
    """
    __tablename__ = "contract"
    id = Column("contract_id", Integer, primary_key=True)
    program_id = Column(Integer, ForeignKey("program.program_id"), index=True, nullable=True)
    tenant_id = Column(Integer, nullable=True)
    is_app_managed = Column(Boolean, default=True)  # see Party.is_app_managed
    # Output Template this contract is bound to (1 contract → 1 template)
    output_template_id = Column(Integer, nullable=True, index=True)
    # Phase 1: the schedule/sheet this contract covers (e.g. "Schedule A"). Lets
    # ONE program hold many active contracts — one active per (program, schedule).
    # NULL for legacy contracts (keeps the old "one active per program" behaviour).
    # Requires migration 11_contract_schedule_key.sql.
    schedule_key = Column(String, nullable=True, index=True)
    filename = Column(String, nullable=True)
    status = Column("status_ops", String, default="drafted")
    extracted = Column(JSON, nullable=True)
    # Contract clause → Output Template field mappings produced by LLM
    template_field_mappings = Column(JSON, nullable=True)
    blob = Column(LargeBinary, nullable=True)
    # Blob-storage pointer (Azure/Azurite) for the raw contract file. Previously
    # the uploaded file was discarded after extraction; when blob storage is on
    # it is now retained here.
    blob_ref = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SheetBinding(Base):
    """Phase 2: the SAVED default binding for a BDX format (mapper). One row per
    (mapper, sheet): which schedule / contract / output template a sheet maps to,
    which sheets it depends on, and its role. Reused each upload; overridable.
    Requires migration 12_sheet_bindings.sql."""
    __tablename__ = "bdx_sheet_binding"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True, index=True)
    mapper_id = Column(Integer, nullable=True, index=True)   # format identity
    program_id = Column(Integer, nullable=True, index=True)
    sheet_name = Column(String, nullable=False)
    role = Column(String, default="schedule")   # schedule|summary|check|supplement|ignore
    schedule_key = Column(String, nullable=True)
    contract_id = Column(Integer, nullable=True)
    output_template_id = Column(Integer, nullable=True)
    depends_on = Column(JSON, nullable=True)             # [sheet_name, ...]
    reference_doc_ids = Column(JSON, nullable=True)      # [reference_document.id, ...]
    supplement_source_id = Column(Integer, nullable=True)
    approved = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class UploadSheetContract(Base):
    """Phase 2: per-upload snapshot of what each sheet was actually ingested/
    validated against (lineage). Resolved from bindings at ingest; overridable.
    Requires migration 12_sheet_bindings.sql."""
    __tablename__ = "upload_sheet_contract"
    id = Column(Integer, primary_key=True)
    upload_id = Column(Integer, nullable=True, index=True)
    sheet_name = Column(String, nullable=False)
    schedule_key = Column(String, nullable=True)
    contract_id = Column(Integer, nullable=True)
    output_template_id = Column(Integer, nullable=True)
    was_override = Column(Boolean, default=False)


class ReferenceDocument(Base):
    """Phase 3b: a shared reference/supplement document extracted ONCE and reused
    across sheets/contracts. Requires migration 13_supplement_and_crosssheet.sql."""
    __tablename__ = "reference_document"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True, index=True)
    program_id = Column(Integer, nullable=True, index=True)
    filename = Column(String, nullable=True)
    kind = Column(String, default="reference")   # reference|supplement
    blob = Column(LargeBinary, nullable=True)
    # Blob-storage pointer (Azure/Azurite); see Contract.blob_ref.
    blob_ref = Column(String, nullable=True)
    extracted = Column(JSON, nullable=True)
    fingerprint = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SupplementSource(Base):
    """Phase 3b: a supplement data source (extra column / sheet / file) and how it
    joins to schedule rows (by policy number). Mapping learned once at setup; the
    DATA refreshes monthly. Requires migration 13_supplement_and_crosssheet.sql."""
    __tablename__ = "supplement_source"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True, index=True)
    mapper_id = Column(Integer, nullable=True, index=True)
    kind = Column(String, default="column")   # column|sheet|file
    sheet_name = Column(String, nullable=True)
    join_key = Column(String, default="policy_number")
    columns = Column(JSON, nullable=True)
    reference_document_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class RuleSql(Base):
    """Cached DuckDB SQL for a validation rule (DuckDB validation engine).

    Each contract validation_rule is compiled by the LLM into a read-only SQL
    query that returns the failing rows. We cache the result here keyed by
    rule_id + a hash of the output-template schema it was generated against, so
    the LLM is called only once per rule (regenerated only if the schema or rule
    changes, or generation previously failed).

    status:
      ok              -> sql is validated and runnable
      cannot_process  -> generation/guard failed twice; show a message to the user
    """
    __tablename__ = "rule_sql"
    id = Column(Integer, primary_key=True)
    rule_id = Column(Integer, index=True, nullable=False)
    contract_id = Column(Integer, index=True, nullable=True)
    template_id = Column(Integer, index=True, nullable=True)
    schema_hash = Column(String, nullable=True)
    rule_hash = Column(String, nullable=True)
    sql_text = Column(Text, nullable=True)
    status = Column(String, default="ok")          # ok | cannot_process
    message = Column(Text, nullable=True)           # why it cannot be processed
    attempts = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppUser(Base):
    """Tenant user (S-22). Auth is mock for the POC — `password` holds a bcrypt
    hash (kept for now; real auth/RBAC + dropping password is a later phase,
    see docs/db_repair_notes.md)."""
    __tablename__ = "app_user"
    id = Column("user_id", Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True)
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=False)
    # DB default is 'tenant_user'; existing rows use 'admin'/'ops'. Role
    # vocabulary normalization + CHECK constraints are a deferred phase, so
    # the ORM default stays 'ops' to preserve current behavior for now.
    role = Column(String, default="ops")
    status = Column(String, default="active")
    password = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)  # set on each successful /auth/login
    reset_token = Column(String, nullable=True)               # password-reset token
    reset_token_expires = Column(DateTime(timezone=True), nullable=True)


class ColumnMappingCache(Base):
    """Cross-tenant cache of (sheet, column, sample_fingerprint) → canonical_field.

    Used by the mapper so once a column has been mapped (either by the LLM
    or by a user override) the same column in a future workbook is served
    from cache instead of re-asking Gemini.

    Lookup is tiered (best match first):
      1. exact (sheet_norm, column_norm, sample_fingerprint)
      2. exact (column_norm, sample_fingerprint)
      3. column_norm only — majority vote across cache rows
    """
    __tablename__ = "column_mapping_cache"
    id = Column(Integer, primary_key=True)
    sheet_norm = Column(String, index=True, nullable=True)
    column_norm = Column(String, index=True, nullable=False)
    # Short hash of the first ~5 normalised sample values. Lets us recognise
    # "same column header, same value shape" even on different files.
    sample_fingerprint = Column(String, index=True, nullable=True)
    canonical_field = Column(String, nullable=False)
    confidence = Column(Integer, default=100)          # 0–100, integer for indexing
    source = Column(String, default="llm")             # llm | user | heuristic
    hit_count = Column(Integer, default=1)
    last_seen_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


class ActivityEvent(Base):
    """Lightweight audit feed for S-12 'Recent activity'."""
    __tablename__ = "activity_events"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id (enforced in DB)
    actor = Column(String, nullable=True)
    action = Column(String, nullable=False)
    target = Column(String, nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# --- Direct Input→Output lane ---------------------------------------------
# A second, decoupled pipeline that maps an uploaded input file straight to the
# required output (BDX) — so users review one small input↔output mapping instead
# of two large mappings against the 850-field data model. The data-model load is
# deferred to an admin step, memoised per input format. See
# docs/Direct_Input_to_Output_Mapping_Process.docx.

class DirectFormat(Base):
    """Registry of a recognised input layout for the direct lane.

    One row per (tenant, input column-signature). Stores the *learned* config so
    repeat files of the same format flow straight through without review:
      - `sheet_routing`   how input tabs feed output tabs (pair / merge / split)
      - `column_mapping`  {output_sheet: {output_col: rule}} (copy/const/transform)
      - `candidates`      AI ranked picks per output column (UI picker)
    Plus the deferred data-model state: `datamodel_mapped` flips True once an
    admin has mapped this format to the canonical model (`datamodel_mapper_id`
    → mappers.id), after which ingestion of this format is fully automatic.
    """
    __tablename__ = "direct_format"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id
    name = Column(String, nullable=True)
    fingerprint = Column(String, index=True, nullable=False)  # input signature hash
    output_template_id = Column(Integer, nullable=True, index=True)
    contract_id = Column(Integer, nullable=True)  # legacy/fallback: single contract
    # Per-schedule contracts: {output_sheet_name: contract_id}. Lets one BDX bind a
    # different contract to each schedule sheet. Falls back to contract_id for
    # sheets not listed here. Requires migration 15_direct_format_sheet_contracts.
    sheet_contracts = Column(JSON, nullable=True)
    # Optional supplement config: {join_key, input_sheets:[...], reference_document_id}
    supplement = Column(JSON, nullable=True)
    # Setup is scoped to a (carrier, program) pair: the active approved row for a
    # pair is the binding used when ops uploads a data file for that carrier+program.
    carrier_party_id = Column(Integer, nullable=True, index=True)
    program_id = Column(Integer, nullable=True, index=True)
    sheet_routing = Column(JSON, nullable=True)
    column_mapping = Column(JSON, nullable=True)
    candidates = Column(JSON, nullable=True)
    datamodel_mapped = Column(Boolean, default=False)
    datamodel_mapper_id = Column(Integer, nullable=True)
    approved = Column(Integer, default=0)
    hit_count = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LandingRecord(Base):
    """The single faithful JSON capture of one uploaded input file.

    Both the rendered output BDX and the (deferred) canonical data-model load
    are *projections* of this one record, so what is delivered to the carrier
    and what is stored for analytics can never silently diverge.

    `data` shape:  {"sheets": {sheet_name: {"columns": [...], "rows": [{...}]}}}
    """
    __tablename__ = "landing_record"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id
    format_id = Column(Integer, index=True, nullable=True)  # FK -> direct_format.id
    source_filename = Column(String, nullable=True)
    fingerprint = Column(String, index=True, nullable=True)
    data = Column(JSON, nullable=False)
    row_count = Column(Integer, default=0)
    # deferred data-model load state for this landing
    datamodel_status = Column(String, default="pending")   # pending | loaded | skipped
    canonical_upload_id = Column(Integer, nullable=True, index=True)
    # The generated BDX this landing produced (set by /direct/run & render). Its
    # presence marks this landing as an actual *run* (vs. a setup-sample upload),
    # and links the uploaded file to its output + exceptions in the run history.
    output_export_id = Column(Integer, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class LandingCorrection(Base):
    """A reviewer's decision on ONE output-stage (direct-lane) exception, plus the
    corrected value to apply.

    Direct-lane output BDX is projected from ``landing_record.data`` (raw input),
    NOT the canonical warehouse — so a Fix must be written back here as an OVERRIDE
    on the source cell, applied on top of the raw capture at render time (the raw
    ``landing_record.data`` is never mutated). This row is both the decision record
    (status/reason for Screen A/B) and the override.

    Keyed by (landing_id, output_sheet, output_row, output_field) so it is
    idempotent and survives re-renders of the same landing (a new output_export is
    produced each render, but the landing — and its corrections — persists).
    """
    __tablename__ = "landing_correction"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    landing_id = Column(Integer, index=True, nullable=False)  # FK -> landing_record.id
    # exception coordinates (the decision key) — from the output_exports blob
    output_sheet = Column(String, nullable=True)
    output_row = Column(Integer, nullable=True)               # 1-based row in the output sheet
    output_field = Column(String, nullable=True)
    rule_id = Column(Integer, nullable=True)
    policy_number = Column(String, nullable=True)             # for display / cross-check
    # the decision
    kind = Column(String, nullable=False)                    # approve | fix | dismiss | reject
    reason = Column(Text, nullable=True)
    # the resolved source cell + override value (fix/approve only)
    input_sheet = Column(String, nullable=True)
    input_row_index = Column(Integer, nullable=True)          # 0-based row in landing_record.data
    source_column = Column(String, nullable=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    decided_by = Column(String, nullable=True)
    decided_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("landing_id", "output_sheet", "output_row", "output_field",
                         name="uq_landing_correction_cell"),
    )


class AdminMappingTask(Base):
    """A queued, one-time request for an admin to map a brand-new input format
    to the 850-field data model. Raised off the delivery critical path; the
    `landing_record_ids` are backfilled into the canonical warehouse once the
    admin approves the mapping."""
    __tablename__ = "admin_mapping_task"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id
    format_id = Column(Integer, index=True, nullable=True)
    fingerprint = Column(String, index=True, nullable=True)
    status = Column(String, default="open")    # open | in_progress | done | dismissed
    title = Column(String, nullable=True)
    detail = Column(JSON, nullable=True)
    landing_record_ids = Column(JSON, nullable=True)
    created_by = Column(String, nullable=True)
    resolved_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    resolved_at = Column(DateTime, nullable=True)


class SubmissionSchedule(Base):
    """Group 3 (C-1/C-5/C-6/C-8) — one row per program: how often this broker owes
    a BDX and when each is due. Derived from the contract by default
    (Program.bdx_frequency + contract.inception_dt); the *_override columns let ops
    set/correct it (C-6). When nothing resolves, no calendar is built (never guess).
    """
    __tablename__ = "submission_schedule"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    program_id = Column(Integer, index=True, nullable=False)
    contract_id = Column(Integer, index=True, nullable=True)
    frequency_override = Column(String, nullable=True)         # 'weekly'|'monthly'|'quarterly'
    anchor_date_override = Column(Date, nullable=True)
    due_offset_days = Column(Integer, default=10)
    grace_days = Column(Integer, default=3)
    soon_window_days = Column(Integer, default=5)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("program_id", name="uq_submission_schedule_program"),)


class ExpectedSubmission(Base):
    """Group 3 — one generated calendar row: a period the broker must submit a BDX
    for, its computed due date, and a status recomputed from the due date +
    grace/soon windows and whether a file was received for the period.
    """
    __tablename__ = "expected_submission"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    program_id = Column(Integer, index=True, nullable=False)
    schedule_id = Column(Integer, index=True, nullable=True)
    period = Column(String, nullable=False)                    # '2026-01' | '2026-Q1' | '2026-W03'
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False, index=True)
    status = Column(String, default="scheduled", index=True)
    received_at = Column(Date, nullable=True)
    received_export_id = Column(Integer, nullable=True)
    # The overdue bell fired for this row — separate from `status` (recomputed on
    # read) so the reminder fires once and survives a re-materialize.
    overdue_notified = Column(Boolean, default=False)
    # C-9: the "due soon" bell fired for this row (once, when it entered the soon
    # window before the due date) — the reminder BEFORE it's late.
    due_soon_notified = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("program_id", "period", name="uq_expected_submission_program_period"),
    )


def _ensure_column(conn, inspector, table: str, column: str, ddl_type: str) -> None:
    """Add `column` to `table` if it isn't already there. Dialect-agnostic
    column inspection so this works on both SQLite and PostgreSQL."""
    try:
        existing = {c["name"] for c in inspector.get_columns(table)}
    except Exception:
        # Table doesn't exist yet — create_all will have built it correctly.
        return
    if column in existing:
        return
    conn.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN {column} {ddl_type}')


def init_db():
    # Under RLS mode the app connects as the DML-only `kavachio_app` role, which
    # cannot run DDL — and auto-create_all() at import is exactly what resurrects
    # dropped tables on the shared DB. So skip all schema bootstrap in RLS mode;
    # schema is then owned by the migration files (docs/migrations), not the app.
    if RLS_ENABLED:
        return
    # Ops/mapper tables on the local (ops) database.
    Base.metadata.create_all(engine)
    # Canonical 53-table xlsx-derived schema on the canonical Postgres DB.
    canonical_metadata.create_all(canonical_engine)

    # Lightweight migrations for columns added after the initial schema.
    # Done with SQLAlchemy inspector so it works on SQLite AND Postgres.
    dialect = engine.dialect.name           # "sqlite", "postgresql", …
    blob_type = "BYTEA" if dialect == "postgresql" else "BLOB"
    json_type = "JSONB" if dialect == "postgresql" else "JSON"
    with engine.begin() as conn:
        inspector = inspect(conn)
        _ensure_column(conn, inspector, "mappers", "spec_by_sheet", json_type)
        _ensure_column(conn, inspector, "mappers", "candidates", json_type)
        _ensure_column(conn, inspector, "mappers", "samples", json_type)
        _ensure_column(conn, inspector, "mappers", "output_by_source", json_type)
        _ensure_column(conn, inspector, "mappers", "source_filename", "VARCHAR")
        _ensure_column(conn, inspector, "mappers", "source_blob", blob_type)
        _ensure_column(conn, inspector, "mappers", "selected_sheets", json_type)
        _ensure_column(conn, inspector, "mappers", "fingerprint_id", "INTEGER")
        # template versioning (input mappers + output templates)
        _ensure_column(conn, inspector, "mappers", "name", "VARCHAR")
        _ensure_column(conn, inspector, "mappers", "version", "INTEGER DEFAULT 1")
        _ensure_column(conn, inspector, "mappers", "is_active", "INTEGER DEFAULT 0")
        _ensure_column(conn, inspector, "export_templates", "version", "INTEGER DEFAULT 1")
        _ensure_column(conn, inspector, "export_templates", "is_active", "INTEGER DEFAULT 0")
        _ensure_column(conn, inspector, "export_templates", "carrier_party_id", "INTEGER")
        _ensure_column(conn, inspector, "export_templates", "contract_id", "INTEGER")
        # _ensure_column(conn, inspector, "program", "canonical_program_id", "INTEGER")
        # tenant_configs folded into `tenant` and dropped (see db_repair_notes.md)
        _ensure_column(conn, inspector, "dwh_bdx", "sheet_name", "VARCHAR")
        _ensure_column(conn, inspector, "dwh_bdx", "upload_id", "INTEGER")
        _ensure_column(conn, inspector, "upload", "source_blob", blob_type)
        _ensure_column(conn, inspector, "export_templates", "template_blob", blob_type)
        # Blob-storage pointers (Azure/Azurite). Nullable strings added alongside
        # the legacy LargeBinary columns so old rows still resolve from the DB.
        _ensure_column(conn, inspector, "mappers", "source_blob_ref", "VARCHAR")
        _ensure_column(conn, inspector, "upload", "source_blob_ref", "VARCHAR")
        _ensure_column(conn, inspector, "output_exports", "blob_ref", "VARCHAR")
        _ensure_column(conn, inspector, "contract", "blob_ref", "VARCHAR")
        _ensure_column(conn, inspector, "reference_document", "blob_ref", "VARCHAR")
        _ensure_column(conn, inspector, "export_templates", "template_blob_ref", "VARCHAR")
        _ensure_column(conn, inspector, "program", "party_id", "INTEGER")
        _ensure_column(conn, inspector, "mappers", "party_id", "INTEGER")
        _ensure_column(conn, inspector, "upload", "party_id", "INTEGER")
        # Contract → Output Template hierarchy
        _ensure_column(conn, inspector, "contract", "output_template_id", "INTEGER")
        _ensure_column(conn, inspector, "contract", "template_field_mappings", json_type)
        # Direct-lane setup scoping (carrier + program)
        _ensure_column(conn, inspector, "direct_format", "carrier_party_id", "INTEGER")
        _ensure_column(conn, inspector, "direct_format", "program_id", "INTEGER")
        _ensure_column(conn, inspector, "landing_record", "output_export_id", "INTEGER")
        # Group 3: added to expected_submission after the table existed.
        _ensure_column(conn, inspector, "expected_submission", "overdue_notified",
                       "BOOLEAN DEFAULT FALSE")
        # C-9: the "due soon" reminder flag (fires once before the due date).
        _ensure_column(conn, inspector, "expected_submission", "due_soon_notified",
                       "BOOLEAN DEFAULT FALSE")
        _ensure_column(conn, inspector, "app_user", "last_login_at", "TIMESTAMP")
        _ensure_column(conn, inspector, "app_user", "reset_token", "VARCHAR")
        _ensure_column(conn, inspector, "app_user", "reset_token_expires", "TIMESTAMPTZ")

    # Canonical: add `extras` JSONB column to each row-strategy entity table
    # so user-defined fields (the `_xf:*` mapping prefix) have a home.
    try:
        from canonical import EXTRA_FIELD_ENTITY_TABLES
        with canonical_engine.begin() as cconn:
            c_inspector = inspect(cconn)
            c_json = "JSONB" if canonical_engine.dialect.name == "postgresql" else "JSON"
            for t_name in EXTRA_FIELD_ENTITY_TABLES:
                _ensure_column(cconn, c_inspector, t_name, "extras", c_json)
    except Exception as e:
        # Don't crash app startup if the canonical DB is briefly unreachable —
        # the inspector / ALTERs will run again on next boot.
        import logging
        logging.getLogger("bdx.db").warning(
            "Could not ensure canonical 'extras' columns: %s", e)
        # cols_mappers = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(mappers)").fetchall()}
        # if "spec_by_sheet" not in cols_mappers:
        #     conn.exec_driver_sql("ALTER TABLE mappers ADD COLUMN spec_by_sheet JSON")
        # if "candidates" not in cols_mappers:
        #     conn.exec_driver_sql("ALTER TABLE mappers ADD COLUMN candidates JSON")
        # try:
        #     cols_tc = {r[1] for r in conn.exec_driver_sql(
        #         "PRAGMA table_info(tenant_configs)").fetchall()}
        #     if cols_tc and "currency" not in cols_tc:
        #         conn.exec_driver_sql(
        #             "ALTER TABLE tenant_configs ADD COLUMN currency VARCHAR")
        # except Exception:
        #     pass
        # try:
        #     cols_prog = {r[1] for r in conn.exec_driver_sql(
        #         "PRAGMA table_info(programs)").fetchall()}
        #     if cols_prog and "canonical_program_id" not in cols_prog:
        #         conn.exec_driver_sql(
        #             "ALTER TABLE programs ADD COLUMN canonical_program_id INTEGER")
        # except Exception:
        #     pass
        # cols_bdx = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(dwh_bdx)").fetchall()}
        # if "sheet_name" not in cols_bdx:
        #     conn.exec_driver_sql("ALTER TABLE dwh_bdx ADD COLUMN sheet_name VARCHAR")
        # if "upload_id" not in cols_bdx:
        #     conn.exec_driver_sql("ALTER TABLE dwh_bdx ADD COLUMN upload_id INTEGER")
        # # ExportTemplate: migrate new template_blob column if pre-existing.
        # try:
        #     cols_tpl = {r[1] for r in conn.exec_driver_sql(
        #         "PRAGMA table_info(export_templates)").fetchall()}
        #     if cols_tpl and "template_blob" not in cols_tpl:
        #         conn.exec_driver_sql(
        #             "ALTER TABLE export_templates ADD COLUMN template_blob BLOB")
        # except Exception:
        #     pass
