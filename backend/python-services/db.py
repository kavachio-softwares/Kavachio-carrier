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
  DATABASE_URL  e.g. postgresql+psycopg2://<user>:<password>@<host>:5432/kavachio
"""
import json
import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Float, Index, Integer, String, JSON, DateTime, Date,
    ForeignKey, Text, LargeBinary, Boolean, Numeric, inspect, UniqueConstraint,
    func, text,
)
from sqlalchemy.orm import declarative_base, deferred, sessionmaker, relationship

from canonical import canonical_metadata  # 53 canonical xlsx-derived tables

# Single source of truth.
DATABASE_URL = os.getenv("DATABASE_URL")

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
APP_DB_URL = DATABASE_URL

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

Base = declarative_base()


# ---------------------------------------------------------------------------
# Payload columns: loaded ON DEMAND, never as part of a normal query.
#
# A handful of columns below hold whole files or whole datasets — uploaded
# workbooks, generated xlsx, parsed input rows, exception lists. They dominate
# the database (on one dev tenant: export_templates.template_blob 564 MB,
# landing_record.data 158 MB, output_exports.blob 145 MB, mappers.source_blob
# 114 MB) while almost every query that touches those tables wants a filename,
# a status or a count.
#
# Loading whole ORM entities pulled all of it over the wire and through JSON
# decoding for nothing. `GET /direct/runs` was the visible case — a page of 10
# runs read hundreds of MB to render 4 kB of JSON, and got slower the more
# exceptions a run had — but the same shape existed in every list endpoint over
# these tables.
#
# `deferred()` fixes them all at once, and keeps future queries correct by
# default: the column is left out of the normal SELECT and fetched by a
# targeted follow-up query the first time the attribute is read. Code that
# genuinely wants the bytes (the download/render paths, all of which read them
# inside the owning session) is unchanged and still works — it just pays for
# one extra small SELECT instead of everyone paying for the blob.
#
# The only rule this imposes: read these attributes while the object's Session
# is still open. Detached access raises instead of silently returning stale
# data, which is the safer failure.
def _payload(col):
    """Mark a whole-file / whole-dataset column as load-on-access."""
    return deferred(col)


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
    source_blob = _payload(Column(LargeBinary, nullable=True))
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
    tenant_id = Column(Integer, nullable=False)  # ops tenancy column
    party_id = Column(Integer, nullable=True, index=True)
    mapper_id = Column(Integer, ForeignKey("mappers.id"), nullable=True)
    source_file = Column("upload_filename", String)
    sheets = Column(JSON)               # list of sheet names parsed
    counts_by_sheet = Column(JSON)      # {sheet: row_count}
    total_rows = Column("upload_rows_total", Integer, default=0)
    source_blob = _payload(Column(LargeBinary, nullable=True))
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
    # An output template is agreed at CARRIER + PROGRAMME + BROKER + CONTRACT —
    # the same four levels a contract already sits at. contract_id alone implies
    # the other three (a contract carries its program_id and broker_party_id),
    # but they are stored here too so a template can be resolved for a scope in
    # one indexed read, and so a template can exist ABOVE contract level (broker
    # set, contract NULL) as a default for that broker's contracts.
    # All four are NULLABLE: templates created before this existed keep working
    # at (carrier, program) scope — see _resolve_output_template.
    program_id = Column(Integer, nullable=True, index=True)
    broker_party_id = Column(Integer, nullable=True, index=True)
    # Where this template's field list came from, so the editor and the
    # validator know which rules apply to it:
    #   uploaded  — the user's own sample workbook (the original, default path)
    #   standard  — generated from a bundled reporting standard (Lloyd's v5.2)
    #   contract  — generated from the contract's extracted clauses + the
    #               standard field library
    source_kind = Column(String, nullable=True)
    # Provenance for a `standard` template: {"standard","version","jurisdiction"}.
    # Read back by the validator to decide which fields are mandatory. Also set
    # on `contract` templates to record which standard supplied the base fields.
    standard_meta = Column(JSON, nullable=True)
    # The full output layout — every sheet AND every column definition. The
    # per-sheet `columns` list is ~99% of it (half a MB for an 11-sheet
    # template), and only the single-template endpoints ever render it, so it
    # loads on access like the other payload columns. List endpoints use
    # _structure_summaries() in main.py, which strips `columns` in SQL.
    structure = _payload(Column(JSON, nullable=False))
    # Output file format this template produces: xlsx | csv | xml | json.
    # xlsx (the default) preserves the sample workbook's styling; the others
    # serialize the mapped rows via output_serializers.
    output_format = Column(String, default="xlsx")
    # Original sample workbook bytes — used to preserve fonts, colors, borders,
    # column widths, merged cells, etc. when generating output.
    template_blob = _payload(Column(LargeBinary, nullable=True))
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
    # WHICH VERSION of that template produced this file. A template can be
    # edited after a file was generated from it; without this the January
    # download would silently start describing itself with February's layout.
    template_version = Column(Integer, nullable=True)
    # The scope the run was for, denormalised so history can be filtered and
    # re-read without walking back through the pipeline (which can be edited,
    # superseded or deleted). NULL on every export generated before this.
    pipeline_id = Column(Integer, nullable=True, index=True)
    carrier_party_id = Column(Integer, nullable=True, index=True)
    program_id = Column(Integer, nullable=True, index=True)
    broker_party_id = Column(Integer, nullable=True, index=True)
    contract_id = Column(Integer, nullable=True, index=True)
    # The format actually written (xlsx | csv | xml | json) — the template's
    # output_format at generation time, which can change afterwards.
    output_format = Column(String, nullable=True)
    # Result of checking the generated file against the template's sample
    # workbook: {"status", "checked", "issues":[...]}; NULL when no sample was
    # configured, which is not a failure — see plan section 14.
    sample_comparison = _payload(Column(JSON, nullable=True))
    filename = Column(String, nullable=False)
    source_upload_id = Column(Integer, nullable=True)
    policy_ids = Column(JSON, nullable=True)          # canonical policy_ids exported
    generated_by = Column(String, nullable=True)      # actor email
    policy_count = Column(Integer, default=0)
    exception_count = Column(Integer, default=0)
    exceptions = _payload(Column(JSON, nullable=True))          # [{severity, code, sheet, row, field, message}]
    # Per-severity totals of `exceptions`, denormalized at write time (and
    # backfilled by init_db) so the admin dashboard can SUM a date window in
    # SQL instead of parsing every multi-MB exceptions blob per request.
    critical_count = Column(Integer, nullable=True)
    warning_count = Column(Integer, nullable=True)
    info_count = Column(Integer, nullable=True)
    status = Column(String, default="clean")          # clean | has_exceptions
    blob = _payload(Column(LargeBinary, nullable=True))         # the generated xlsx
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
    # v4: tenant_code is the unique business identifier (holds the legacy mga
    # code); tenant_legal_name is the display/legal name.
    tenant_name = Column("tenant_code", String, unique=True, index=True, nullable=False)
    legal_name = Column("tenant_legal_name", String, nullable=True)
    tenant_type = Column(String, nullable=True)
    # --- operational columns (outside the canonical model) -----------------
    address = Column(JSON, nullable=True)
    currency = Column(String, nullable=True)      # ISO 4217 (USD, EUR, GBP …)
    logo = Column(Text, nullable=True)            # org logo as a data URL (sidebar co-brand)
    internal_codes = Column(JSON, nullable=True)  # JSONB free-form
    is_active = Column("tenant_is_active", Boolean, default=True)
    # Set once the tenant admin explicitly dismisses the first-login onboarding
    # wizard ("Skip for now"). Without this, needs_onboarding (below) is purely
    # derived from setup state, so a tenant that skips before finishing Bordereau
    # Setup would be bounced back to /welcome on every subsequent login.
    onboarding_skipped = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class GenericRuleSpecification(Base):
    """A managed rule-library entry — Kavachio's standard BDX checks.

    Two scopes, keyed on `tenant_id`:
      • tenant_id IS NULL  → GLOBAL rule. Managed by kavachio_admin, applied to
        every tenant's uploads.
      • tenant_id = <id>   → TENANT rule. Managed by that tenant's tenant_admin,
        applied only to that tenant's uploads and invisible to other tenants.

    Read at contract-upload time by
    contract_upload_services.generic_rule_library.load_generic_rules(tenant_id),
    which loads globals + the current tenant's own active rules. `class_name`
    must be one of the supported operator classes (see that module's
    SUPPORTED_CLASSES) or the rule generates nothing.
    """
    __tablename__ = "generic_rule_spec"
    id = Column("generic_rule_id", Integer, primary_key=True)
    rule_name = Column("generic_rule_name", Text, nullable=False)
    severity = Column("generic_rule_severity", String, nullable=False, default="Major")
    class_name = Column("generic_rule_class_name", String, nullable=False)
    validation_logic = Column("generic_rule_logic", Text, nullable=True)
    is_generic = Column(Boolean, nullable=False, default=True)   # ops column
    # NULL = global (all tenants); a tenant_id = private to that tenant.
    tenant_id = Column("generic_rule_tenant_id", Integer,
                       ForeignKey("tenant.tenant_id"), nullable=True, index=True)
    is_active = Column("generic_rule_is_active", Boolean, nullable=False, default=True)
    created_by = Column(Integer, nullable=True)                  # app_user.id of the author
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Party(Base):
    """Counterparty / insured / carrier directory entry (S-03 / S-03a)."""
    __tablename__ = "party"
    id = Column("party_id", Integer, primary_key=True)
    tenant_id = Column("party_tenant_id", Integer, nullable=True)
    party_type = Column(String, nullable=False)
    legal_name = Column("party_legal_name", String, nullable=False, index=True)
    # Stable business identifier (v4). The ingester keys synthetic parties on it.
    reference = Column("party_reference", String, nullable=True, index=True)
    tax_id = Column("party_tax_id", String, nullable=True)
    naic_company_code = Column("party_naic_company_code", String, nullable=True)
    address_line1 = Column("party_address_line1", String, nullable=True)
    city = Column("party_city", String, nullable=True)
    subdivision = Column("party_subdivision", String, nullable=True)
    postal_code = Column("party_postal_code", String, nullable=True)
    country = Column("party_country", String, nullable=True)
    sanctions_status = Column("party_sanctions_status", String, nullable=True)
    verified_by_user_id = Column("party_verified_by_user_id", Integer, nullable=True)
    is_active = Column("party_is_active", Boolean, default=True)
    extras = Column("party_extras", JSON, nullable=True)
    # --- operational columns (outside the canonical model) -----------------
    # True = created via the app (the directory shows these); False = BDX-ingested.
    # v4 sends bordereau-read names to ingested_party instead, but this marker
    # still separates curated rows from ingester-minted ambient parties.
    is_app_managed = Column(Boolean, default=True)
    scope = Column(String, default="tenant")
    dba_name = Column(String, nullable=True)
    naics_code = Column(String, nullable=True)
    am_best_rating = Column(String, nullable=True)
    domicile_country = Column(String, nullable=True)
    primary_jurisdiction = Column(String, nullable=True)
    addresses = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # --- carrier hierarchy -------------------------------------------------
    # "Has this broker actually come on board?" — not_invited | invited |
    # active | suspended, NULL for parties that are not producers.
    #
    # READ-ONLY from here. It is derived from app_user by the DB triggers
    # trg_set_broker_onboarding / trg_refresh_broker_onboarding, so assigning
    # it in Python is silently overruled on the way to the table. Nothing in
    # the app should ever write it — change the people, and this follows.
    onboarding_status = Column(String, nullable=True)


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
    tenant_id = Column("program_tenant_id", Integer, nullable=True)
    name = Column("program_name", String, nullable=False)
    bdx_frequency = Column("program_bordereau_frequency", String, nullable=True)
    business_segment = Column("program_business_segment", String, nullable=True)
    product_line = Column("program_product_line", String, nullable=True)
    annual_statement_lob = Column("program_annual_statement_line_of_business",
                                  String, nullable=True)
    due_after_days = Column("program_due_after_days", Integer, nullable=True)
    canonical_status = Column("program_status", String, nullable=True)
    # --- operational columns (outside the canonical model) -----------------
    is_app_managed = Column(Boolean, default=True)  # see Party.is_app_managed
    party_id = Column(Integer, ForeignKey("party.party_id"), nullable=True, index=True)
    lead_carrier = Column(String, nullable=True)
    admin_party = Column(String, nullable=True)
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
    program_id = Column("contract_program_id", Integer,
                        ForeignKey("program.program_id"), index=True, nullable=True)
    tenant_id = Column(Integer, nullable=True)  # ops tenancy column (see canonical.tenant_col)
    is_app_managed = Column(Boolean, default=True)  # see Party.is_app_managed
    # Output Template this contract is bound to (1 contract → 1 template)
    output_template_id = Column(Integer, nullable=True, index=True)
    # Phase 1: the schedule/sheet this contract covers (e.g. "Schedule A"). Lets
    # ONE program hold many active contracts — one active per (program, schedule).
    # NULL for legacy contracts (keeps the old "one active per program" behaviour).
    # Requires migration 11_contract_schedule_key.sql.
    schedule_key = Column(String, nullable=True, index=True)
    # The contract's own term. These columns already EXIST in the canonical
    # schema and are written on every contract upload (contract_upload_services/
    # db_persister.py INSERTs inception_dt/expiry_dt from the extracted
    # program_metadata) — they were simply never mapped on the ORM, so the app
    # could only reach inception by digging through `extracted`. Mapping them
    # here is a read-side addition: NO migration, no new column.
    # expiry_dt is what the Program Management screen calls the "term end" — the
    # date a continuation / renewal decision is due.
    inception_dt = Column("contract_inception_date", Date, nullable=True)
    expiry_dt = Column("contract_expiry_date", Date, nullable=True)
    filename = Column(String, nullable=True)
    status = Column("status_ops", String, default="drafted")
    extracted = Column(JSON, nullable=True)
    # Contract clause → Output Template field mappings produced by LLM
    template_field_mappings = Column(JSON, nullable=True)
    blob = _payload(Column(LargeBinary, nullable=True))
    # Blob-storage pointer (Azure/Azurite) for the raw contract file. Previously
    # the uploaded file was discarded after extraction; when blob storage is on
    # it is now retained here.
    blob_ref = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # --- carrier hierarchy -------------------------------------------------
    # Which broker holds this contract with the carrier. A contract is always
    # (programme x broker); program_broker says that pair is allowed at all.
    broker_party_id = Column("contract_broker_party_id", Integer,
                             ForeignKey("party.party_id"), nullable=True, index=True)
    # The ONE approval in the platform. A contract a BROKER uploads waits for
    # THE APPROVAL GATE IS GONE, and so is the broker-side upload it policed.
    # `contract_approval_status`, `contract_approved_by_id` and
    # `contract_approved_at` are left in the database — the schema is shared, and
    # dropping a column is not this application's to do — but nothing reads or
    # writes them any more, so they are not mapped here. An unmapped column
    # cannot be revived by accident; a mapped one can.
    submitted_by_user_id = Column("contract_submitted_by_id", Integer,
                                  ForeignKey("app_user.user_id"), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    # The agreed most-premium-they-may-write for the term. A LIMIT from the
    # wording — deliberately not an estimate, which is a forecast.
    premium_cap_amount = Column("contract_premium_cap_amount", Numeric, nullable=True)
    premium_cap_currency = Column(String, nullable=True)

    # --- the contract as a RECORD ------------------------------------------
    # Every column below already exists in the canonical schema and was simply
    # never mapped, so the app could only ever show a filename where it meant
    # to show a contract. Mapping them is a read/write-side addition with NO
    # migration. What they mean, and which are mandatory, is decided per
    # contract type in contract_types.py — never here.
    name = Column("contract_name", String, nullable=True)
    # insurer_broker | insurer_reinsurer. Free text in the DB (there is no CHECK
    # on the column) but constrained by contract_types.spec() on the way in, so
    # the legacy free-text values already in the table still load.
    contract_type = Column("contract_type", String, nullable=True)
    # The market's identifier for a delegated authority. NOTE the bare `umr`
    # column on this table is legacy and unused (all NULL) — the ingester and
    # the persister both write contract_primary_umr, so that is the one mapped.
    umr = Column("contract_primary_umr", String, nullable=True)
    risk_code = Column("contract_risk_code", String, nullable=True)
    section_number = Column("contract_section_number", String, nullable=True)
    class_of_business = Column("contract_class_of_business", String, nullable=True)
    year_of_account = Column("contract_year_of_account", String, nullable=True)
    earnings_pattern = Column("contract_earnings_pattern", String, nullable=True)
    executed_date = Column("contract_executed_date", Date, nullable=True)
    notice_period_days = Column("contract_notice_period_days", Integer, nullable=True)

    # --- lifecycle ---------------------------------------------------------
    # TWO status axes, because they answer two different questions:
    #   status_ops       what the extraction PIPELINE did with the file.
    #   lifecycle        where the CONTRACT itself is — see contract_types.
    # Collapsing them would lose one: a contract can be extracted and still not
    # in force because its term has not started.
    lifecycle = Column("contract_status", String, nullable=True)
    lifecycle_effective_date = Column("contract_status_effective_date", Date, nullable=True)
    terminated_date = Column("contract_terminated_date", Date, nullable=True)
    termination_reason = Column("contract_termination_reason", Text, nullable=True)
    # The contract this one renews. A renewal is a NEW row pointing back, not an
    # edit to the old term — last year's contract has to keep meaning what it
    # meant when it was produced against.
    renews_contract_id = Column("contract_renews_contract_id", Integer,
                                ForeignKey("contract.contract_id"), nullable=True)

    # --- the authored contract -------------------------------------------
    # What the two sides agreed to pay each other (commission, shares, fees,
    # settlement) as one dict — see contract_types.COMMERCIAL_TERMS for the
    # vocabulary and for why this is JSON and not eleven columns. Used twice:
    # shown on the record, and quoted verbatim inside the generated wording.
    commercial_terms = Column(JSON, nullable=True)
    # The wording's sections as the carrier left them — generated ones edited
    # or not, plus any written by hand — and the signature-page layout. The
    # composed .docx is built FROM this, so the document can always be
    # regenerated and the sections are never trapped inside a binary.
    wording_sections = Column(JSON, nullable=True)


class ContractDocument(Base):
    """The documents a contract is made of — and which of them rules come from.

    Until now a contract WAS its file: one blob on the contract row. That cannot
    express the three things this flow needs.

      contract     the wording itself. Optional, because a contract can now be
                   raised from its terms before anyone has the executed PDF.
      reference    a document the wording DEFERS to ("per the Purchasing
                   Guidelines on file"). The clauses that point at it produce no
                   usable rule until it is supplied, which is why it is
                   mandatory once the extraction names one.
      endorsement  a change agreed after the fact. It does NOT replace the
                   wording — both stay active, and rule generation reads the
                   pair, with the endorsement's clauses taking precedence over
                   the ones they amend.

    `is_active` is what rule generation filters on, so superseding an
    endorsement is a flag flip rather than a delete: the rules it produced stay
    explainable afterwards.

    This maps the EXISTING canonical `contract_document` table rather than a new
    one of its own. The table was already in the v4 schema — defined, empty, and
    reached by nothing in the app — and it is the right shape for this: a
    document, its type, its version, who uploaded it, and whether it is the
    executed copy. Five columns it lacks (the ones this flow turns on) are added
    by migration 14; everything else was already there and is used as it stands.
    """
    __tablename__ = "contract_document"
    id = Column("contract_document_id", Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True, index=True)
    contract_id = Column("contract_document_contract_id", Integer,
                         ForeignKey("contract.contract_id"),
                         nullable=False, index=True)
    # contract | reference | endorsement. The canonical column is named for the
    # document's TYPE, which is exactly what this is.
    kind = Column("contract_document_type", String, nullable=False,
                  default="contract")
    filename = Column("contract_document_filename", String, nullable=True)
    version = Column("contract_document_version", String, nullable=True)
    # Whether this is the signed copy rather than a draft. Kavachio does not run
    # the signing, so this records a fact from elsewhere.
    is_executed_copy = Column("contract_document_is_executed_copy", Boolean,
                              nullable=True)
    blob_ref = Column("contract_document_blob_reference", String, nullable=True)
    fingerprint = Column("contract_document_file_hash", String, nullable=True)
    uploaded_by_user_id = Column("contract_document_uploaded_by_id", Integer,
                                 nullable=True)
    amendment_id = Column("contract_document_amendment_id", Integer, nullable=True)
    upload_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow,
                         onupdate=datetime.utcnow)

    # --- added by migration 14 ---------------------------------------------
    # The document the wording asked for BY NAME, when this row answers such a
    # request. Lets the "still missing" list be computed by matching what the
    # extraction named against what has actually been supplied.
    satisfies_reference = Column("contract_document_satisfies_reference",
                                 String, nullable=True)
    # Endorsements carry a date they take effect from; the wording does not.
    effective_from = Column("contract_document_effective_from", Date, nullable=True)
    # What rule generation filters on. Superseding is a flag flip, never a
    # delete — see the class docstring.
    is_active = Column("contract_document_is_active", Boolean, default=True)
    # Parsed text + whatever the extractor made of it, cached so re-running rule
    # generation does not re-parse every attachment.
    extracted = Column("contract_document_extracted", JSON, nullable=True)
    # The DB-blob fallback for when blob storage is off, matching the
    # (blob_ref, blob) pair every other file-bearing table here uses. The
    # canonical table only ever had the Azure pointer.
    blob = _payload(Column("contract_document_blob", LargeBinary, nullable=True))


class ContractSignature(Base):
    """That a contract was signed, by which side, when, and on what authority.

    A CONTRACT GOES LIVE BECAUSE IT WAS SIGNED. Both sides sign — one row each,
    at least — and the second signature is what puts it in force. That is why
    this is a table and not a key in the wording blob: it is dated, attributed,
    and it is the thing the contract's being in force now rests on. A key in a
    JSON bag can be overwritten by an unrelated save; a row cannot.

    KAVACHIO DOES NOT WITNESS A SIGNING. It records that one happened, and
    `method` is what keeps the two claims apart:

      typed     the signatory was in Kavachio, was the right party, and typed
                their name against this contract. `by_user_id` IS the signatory.
      recorded  somebody signed on paper or through a provider elsewhere and
                the carrier recorded the fact here. `by_user_id` is whoever
                recorded it — never the signatory, who was never in this
                system. It is the only honest way to hold a reinsurance
                contract, whose counterparty has no seat here at all.

    A SIGNATURE IS ON A VERSION. Editing a draft's terms or its wording deletes
    the signatures on it. A signature that outlived the words it was under
    would be worse than no signature at all.
    """
    __tablename__ = "contract_signature"
    id = Column("contract_signature_id", Integer, primary_key=True)
    tenant_id = Column("contract_signature_tenant_id", Integer, nullable=True)
    contract_id = Column("contract_signature_contract_id", Integer,
                         ForeignKey("contract.contract_id"),
                         nullable=False, index=True)
    # Which ORGANISATION this signature is for, not who typed it.
    side = Column("contract_signature_side", String, nullable=False)
    signer_name = Column("contract_signature_signer_name", String, nullable=False)
    signer_title = Column("contract_signature_signer_title", String, nullable=True)
    signer_email = Column("contract_signature_signer_email", String, nullable=True)
    method = Column("contract_signature_method", String, default="typed")
    by_user_id = Column("contract_signature_by_user_id", Integer, nullable=True)
    signed_at = Column("contract_signature_signed_at", DateTime, nullable=True)
    # The executed copy this was read off, where there is one.
    document_id = Column("contract_signature_document_id", Integer, nullable=True)
    note = Column("contract_signature_note", String, nullable=True)
    created_at = Column("contract_signature_created_at", DateTime,
                        default=datetime.utcnow)


class ProgramBroker(Base):
    """Which brokers may produce into which programme — the many-to-many that
    makes the carrier hierarchy work.

    ONE PROGRAMME HAS MANY BROKERS and ONE BROKER IS ON MANY PROGRAMMES, so the
    pair lives in its own table rather than as a column on either side. It is
    also the gate: a broker with no row here cannot hold a contract on that
    programme, and removing the row stops new work without deleting history.
    """
    __tablename__ = "program_broker"
    id = Column("program_broker_id", Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=True, index=True)  # ops tenancy column
    program_id = Column("program_broker_program_id", Integer,
                        ForeignKey("program.program_id"), nullable=False, index=True)
    broker_party_id = Column("program_broker_party_id", Integer,
                             ForeignKey("party.party_id"), nullable=False, index=True)
    # active | inactive. Never DELETE a pair that has contracts under it —
    # set it inactive so the contracts keep their meaning.
    status = Column("program_broker_status", String, default="active")
    assigned_by_user_id = Column("program_broker_assigned_by_id", Integer, nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContractApproval(Base):
    """Every decision ever made on a contract.

    The contract row holds the CURRENT state; this holds how it got there. A
    contract can be submitted, rejected, re-submitted and approved — "why was
    this rejected in June?" has to stay answerable after the fact.
    """
    __tablename__ = "contract_approval"
    id = Column("approval_id", Integer, primary_key=True)
    tenant_id = Column(Integer, nullable=False, index=True)  # ops tenancy column
    contract_id = Column("approval_contract_id", Integer,
                         ForeignKey("contract.contract_id"), nullable=False, index=True)
    # The decision vocabulary:
    #   submitted | approved | rejected | withdrawn   the broker→carrier gate
    #   sent_for_review | changes_requested | terms_agreed
    #                                               the carrier→broker negotiation
    # One table for both because they are the same kind of fact — somebody did
    # something to this contract, and "how did it get here?" has to stay
    # answerable across both directions of travel.
    action = Column("approval_action", String, nullable=False)
    acted_by_user_id = Column("approval_acted_by_id", Integer, nullable=False)
    acted_at = Column("approval_acted_at", DateTime(timezone=True), default=datetime.utcnow)
    note = Column("approval_note", Text, nullable=True)
    # What the carrier was looking at when it decided. A contract file can be
    # replaced; the decision stays attached to the version it judged.
    contract_file_hash = Column("approval_file_hash", String, nullable=True)
    # A counter-proposal, when this row is one:
    #   [{field, current, proposed, comment}, …]
    # A note alone says "the premium cap is too low" and leaves the carrier to
    # work out which field, what number, and to type it. This says it in terms
    # the other side can see beside the current value and apply in one move —
    # and keeps "what did they actually ask for?" answerable later.
    # Added by migration 15; NULL on every row that proposed nothing.
    proposed_changes = Column("approval_proposed_changes", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)


# ============================================================================
# Create-a-Contract, step 4 — Signatures.
#
# Four tables, one signing round. See migrations/13_contract_esign.sql for the
# reasoning; the short version is the rule that makes the whole thing work:
#
#   a box on the page belongs to ONE signer and says so itself, in
#   `party_key` — 'tenant:<tenant_id>' for the insurer, 'broker:<broker_party_id>'
#   for the broker. The recipient behind the emailed link carries the same
#   string. Field ownership is that equality, checked on the server, never
#   inferred from what the browser posts.
# ============================================================================

def party_key_for_tenant(tenant_id) -> str:
    """The insurer's identity as the fields spell it."""
    return f"tenant:{int(tenant_id)}"


def party_key_for_broker(broker_party_id) -> str:
    """The broker's identity as the fields spell it."""
    return f"broker:{int(broker_party_id)}"


class EsignEnvelope(Base):
    """One document, out for signature once.

    Two PDFs are kept on purpose. `source_pdf` is the wording as it was written
    and is never overwritten; `current_pdf` is that same document with every
    signature applied so far, and it is what the NEXT signer opens — which is
    how the broker sees the insurer's signature already on the page rather than
    a blank block and a promise.
    """
    __tablename__ = "contract_esign_envelope"
    # Indexes are declared here with the SAME NAMES the migration uses, not via
    # `index=True`. init_db() still runs create_all() when RLS is off, so on any
    # given database either this or migrations/13_contract_esign.sql made the
    # table — and an auto-named `ix_contract_esign_envelope_envelope_tenant_id`
    # is not the migration's `ix_esign_envelope_tenant`, so both would be created
    # and every write would maintain two identical indexes. Matching the names
    # makes CREATE INDEX IF NOT EXISTS see what is already there.
    __table_args__ = (
        Index("ix_esign_envelope_tenant", "envelope_tenant_id"),
        Index("ix_esign_envelope_contract", "envelope_contract_id"),
    )
    id = Column("envelope_id", Integer, primary_key=True)
    tenant_id = Column("envelope_tenant_id", Integer, nullable=False)
    # NULL while the contract row does not exist yet: the wizard can send a
    # generated draft before it is filed.
    contract_id = Column("envelope_contract_id", Integer, nullable=True)
    program_id = Column("envelope_program_id", Integer, nullable=True)
    broker_party_id = Column("envelope_broker_party_id", Integer, nullable=True)
    title = Column("envelope_title", String, nullable=False)
    # draft | sent | in_progress | completed | declined | voided
    status = Column("envelope_status", String, nullable=False, default="draft",
                    server_default="draft")
    source_pdf = _payload(Column("envelope_source_pdf", LargeBinary, nullable=True))
    source_pdf_ref = Column("envelope_source_pdf_ref", String, nullable=True)
    current_pdf = _payload(Column("envelope_current_pdf", LargeBinary, nullable=True))
    current_pdf_ref = Column("envelope_current_pdf_ref", String, nullable=True)
    page_count = Column("envelope_page_count", Integer, nullable=False, default=0,
                        server_default=text("0"))
    # Bumped on every stamp so a page image can never be served stale.
    pdf_version = Column("envelope_pdf_version", Integer, nullable=False, default=1,
                         server_default=text("1"))
    created_by_user_id = Column("envelope_created_by_id", Integer, nullable=True)
    created_at = Column("envelope_created_at", DateTime(timezone=True), nullable=False,
                        default=datetime.utcnow, server_default=func.now())
    sent_at = Column("envelope_sent_at", DateTime(timezone=True), nullable=True)
    completed_at = Column("envelope_completed_at", DateTime(timezone=True), nullable=True)

    recipients = relationship(
        "EsignRecipient", back_populates="envelope", cascade="all, delete-orphan",
        order_by="EsignRecipient.order_no")
    fields = relationship(
        "EsignField", back_populates="envelope", cascade="all, delete-orphan")


class EsignRecipient(Base):
    """One organisation's signer on one envelope, and the link that reaches them.

    `party_key` is the identity the boxes are matched against. `tenant_id` and
    `broker_party_id` mirror it in FK-able form, and exactly one is set — the
    same rule chk_app_user_scope enforces on app_user, for the same reason: an
    organisation is a carrier or a broker, never both at once.
    """
    __tablename__ = "contract_esign_recipient"
    __table_args__ = (
        # One organisation signs once per envelope: a duplicated signer row would
        # silently give one side two sets of boxes.
        Index("uq_esign_recipient_party", "recipient_envelope_id",
              "recipient_party_key", unique=True),
        Index("ix_esign_recipient_envelope", "recipient_envelope_id",
              "recipient_order"),
    )
    id = Column("recipient_id", Integer, primary_key=True)
    envelope_id = Column("recipient_envelope_id", Integer,
                         ForeignKey("contract_esign_envelope.envelope_id"),
                         nullable=False)
    side = Column("recipient_side", String, nullable=False)          # insurer | broker
    party_key = Column("recipient_party_key", String, nullable=False)
    tenant_id = Column("recipient_tenant_id", Integer, nullable=True)
    broker_party_id = Column("recipient_broker_party_id", Integer, nullable=True)
    user_id = Column("recipient_user_id", Integer, nullable=True)
    name = Column("recipient_name", String, nullable=False)
    email = Column("recipient_email", String, nullable=False)
    title = Column("recipient_title", String, nullable=True)
    org = Column("recipient_org", String, nullable=True)
    # 1 signs first. The next person is emailed only once this one is done.
    order_no = Column("recipient_order", Integer, nullable=False, default=1,
                      server_default=text("1"))
    # pending | sent | viewed | signed | declined
    status = Column("recipient_status", String, nullable=False, default="pending",
                    server_default="pending")
    # unique WITHOUT index=True: that pair makes SQLAlchemy build a unique INDEX
    # named ix_…, where the migration declares `recipient_token TEXT UNIQUE` and
    # gets the constraint …_recipient_token_key. Same guarantee, different
    # object — and two of them on one column if both sides ran.
    token = Column("recipient_token", String, unique=True, nullable=True)
    token_expires = Column("recipient_token_expires", DateTime(timezone=True), nullable=True)
    sent_at = Column("recipient_sent_at", DateTime(timezone=True), nullable=True)
    viewed_at = Column("recipient_viewed_at", DateTime(timezone=True), nullable=True)
    signed_at = Column("recipient_signed_at", DateTime(timezone=True), nullable=True)
    decline_reason = Column("recipient_decline_reason", Text, nullable=True)
    signature_name = Column("recipient_signature_name", String, nullable=True)
    # The drawn signature as a data URL. Big, and only needed while stamping,
    # so it never rides along on a list query.
    signature_image = _payload(Column("recipient_signature_image", Text, nullable=True))
    signed_ip = Column("recipient_signed_ip", String, nullable=True)
    signed_agent = Column("recipient_signed_agent", String, nullable=True)
    # --- the one-time code that unlocks the link (migration 17) ------------
    # Holding the URL is not enough on its own: a URL leaks through history,
    # chat, screen shares and forwarded mail in ways a mailbox does not. Stored
    # as a bcrypt hash, never in plain text — a readable signing code is a
    # readable signing code to anyone with a database backup.
    otp_hash = Column("recipient_otp_hash", String, nullable=True)
    otp_expires = Column("recipient_otp_expires", DateTime(timezone=True), nullable=True)
    # Six digits is a small space, so these two are the real protection, not the
    # hash: five wrong guesses locks the link for fifteen minutes.
    otp_attempts = Column("recipient_otp_attempts", Integer, nullable=False,
                          default=0, server_default=text("0"))
    otp_locked_until = Column("recipient_otp_locked_until", DateTime(timezone=True),
                              nullable=True)
    otp_verified_at = Column("recipient_otp_verified_at", DateTime(timezone=True),
                             nullable=True)
    # So a resend button cannot be turned into a way to mail somebody hundreds
    # of messages.
    otp_sent_count = Column("recipient_otp_sent_count", Integer, nullable=False,
                            default=0, server_default=text("0"))
    otp_last_sent_at = Column("recipient_otp_last_sent_at", DateTime(timezone=True),
                              nullable=True)
    created_at = Column("recipient_created_at", DateTime(timezone=True), nullable=False,
                        default=datetime.utcnow, server_default=func.now())

    envelope = relationship("EsignEnvelope", back_populates="recipients")


class EsignField(Base):
    """A box on the page, owned by exactly one signer.

    Position is a FRACTION of the page (0..1, origin top-left), not points: the
    signing screen renders each page at whatever width the browser gives it, and
    fractions land the box in the same place at every zoom and every DPI.
    """
    __tablename__ = "contract_esign_field"
    __table_args__ = (
        Index("ix_esign_field_envelope", "field_envelope_id", "field_page"),
        Index("ix_esign_field_party", "field_envelope_id", "field_party_key"),
    )
    id = Column("field_id", Integer, primary_key=True)
    envelope_id = Column("field_envelope_id", Integer,
                         ForeignKey("contract_esign_envelope.envelope_id"),
                         nullable=False)
    # WHO OWNS THIS BOX — the entire access rule, in one column.
    party_key = Column("field_party_key", String, nullable=False)
    recipient_id = Column("field_recipient_id", Integer,
                          ForeignKey("contract_esign_recipient.recipient_id"),
                          nullable=True)
    # signature | initial | name | title | date | text
    type = Column("field_type", String, nullable=False)
    page = Column("field_page", Integer, nullable=False)
    x = Column("field_x", Float, nullable=False)
    y = Column("field_y", Float, nullable=False)
    w = Column("field_w", Float, nullable=False)
    h = Column("field_h", Float, nullable=False)
    required = Column("field_required", Boolean, nullable=False, default=True,
                      server_default=text("TRUE"))
    label = Column("field_label", String, nullable=True)
    # The token in the document that put the box here, e.g.
    # '{{signature:tenant:12}}'. Kept so a re-generated document can be re-tagged
    # and the placement reproduced instead of re-drawn by hand.
    anchor = Column("field_anchor", String, nullable=True)
    value = Column("field_value", Text, nullable=True)
    filled_at = Column("field_filled_at", DateTime(timezone=True), nullable=True)
    created_at = Column("field_created_at", DateTime(timezone=True), nullable=False,
                        default=datetime.utcnow, server_default=func.now())

    envelope = relationship("EsignEnvelope", back_populates="fields")


class EsignEvent(Base):
    """What happened, in order.

    The envelope row holds where it got to; this holds how it got there. It is
    printed as the certificate page on the completed PDF, so nothing that
    matters may be left out of it.
    """
    __tablename__ = "contract_esign_event"
    __table_args__ = (
        Index("ix_esign_event_envelope", "event_envelope_id", "event_at"),
    )
    id = Column("event_id", Integer, primary_key=True)
    envelope_id = Column("event_envelope_id", Integer,
                         ForeignKey("contract_esign_envelope.envelope_id"),
                         nullable=False)
    recipient_id = Column("event_recipient_id", Integer, nullable=True)
    # created | sent | delivered | viewed | signed | declined | completed
    # | reminded | voided
    type = Column("event_type", String, nullable=False)
    actor = Column("event_actor", String, nullable=True)
    ip = Column("event_ip", String, nullable=True)
    agent = Column("event_agent", String, nullable=True)
    detail = Column("event_detail", JSON, nullable=True)
    at = Column("event_at", DateTime(timezone=True), nullable=False,
                default=datetime.utcnow, server_default=func.now())


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
    blob = _payload(Column(LargeBinary, nullable=True))
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
    id = Column("rule_sql_id", Integer, primary_key=True)
    rule_id = Column("rule_sql_rule_id", Integer, index=True, nullable=False)
    contract_id = Column("rule_sql_contract_id", Integer, index=True, nullable=True)
    template_id = Column("rule_sql_template_id", Integer, index=True, nullable=True)
    schema_hash = Column("rule_sql_schema_hash", String, nullable=True)
    rule_hash = Column("rule_sql_rule_hash", String, nullable=True)
    sql_text = Column("rule_sql_text", Text, nullable=True)
    status = Column("rule_sql_status", String, default="ok")   # ok | cannot_process
    message = Column("rule_sql_message", Text, nullable=True)  # why it cannot be processed
    attempts = Column("rule_sql_attempts", Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppUser(Base):
    """Tenant user (S-22). Auth is mock for the POC — `password` holds a bcrypt
    hash (kept for now; real auth/RBAC + dropping password is a later phase,
    see docs/db_repair_notes.md)."""
    __tablename__ = "app_user"
    id = Column("user_id", Integer, primary_key=True)
    tenant_id = Column("user_tenant_id", Integer, nullable=True)
    email = Column("user_email", String, unique=True, index=True, nullable=False)
    full_name = Column("user_full_name", String, nullable=False)
    # One of the four: kavachio_admin | carrier_admin | broker_admin | operator.
    # chk_app_user_role rejects anything else, so the old 'ops' default was a
    # trap — any insert that forgot to name a role failed at COMMIT. The
    # fallback is the LEAST-privileged seat, matching normalize_role().
    role = Column("user_role", String, default="operator")
    status = Column("user_status", String, default="active")
    password = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)  # set on each successful /auth/login
    reset_token = Column(String, nullable=True)               # password-reset token
    reset_token_expires = Column(DateTime(timezone=True), nullable=True)
    # Watermark for the platform_notification feed: rows created after this are
    # unread for this user. NULL = nothing read yet. See PlatformNotification.
    notifications_seen_at = Column(DateTime, nullable=True)
    # --- carrier hierarchy -------------------------------------------------
    # A BROKER user (broker_admin / broker_operator) belongs to a broker party,
    # not to a carrier: the same broker produces for several carriers, so its
    # login cannot be pinned to one tenant_id. Carrier and platform users have
    # this NULL and carry tenant_id instead. Exactly one of the two is set —
    # the DB enforces it as chk_app_user_scope.
    broker_party_id = Column("user_broker_party_id", Integer,
                             ForeignKey("party.party_id"), nullable=True, index=True)
    # Who invited this person. A carrier admin invites broker admins; a broker
    # admin invites its own operators. Answers "who let this person in?".
    invited_by_user_id = Column("user_invited_by_id", Integer,
                                ForeignKey("app_user.user_id"), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)


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


class AuthAudit(Base):
    """Security / auth audit trail — login (success & failed), logout, forgot-
    password, password reset and token refresh, with IP + user-agent. Kept apart
    from activity_events because auth events have a distinct retention / alerting
    profile (see Audit_Logging_Full_Details.docx, Part C)."""
    __tablename__ = "auth_audit"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    actor = Column(String, nullable=True)         # email (attempted or actual)
    event = Column(String, nullable=False)        # login_success | login_failed | logout | forgot_request | password_reset | password_changed | token_refresh
    ok = Column(Boolean, nullable=True)
    ip = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class AccessLog(Base):
    """Read / access audit — who viewed, downloaded or exported sensitive output
    or source data. Separate table (high volume, own retention)."""
    __tablename__ = "access_log"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    user_id = Column(Integer, nullable=True)
    actor = Column(String, nullable=True)         # email
    resource = Column(String, nullable=False)     # request path, e.g. /export/downloads/279/file
    action = Column(String, nullable=False)       # download | read | export
    ip = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class PlatformNotification(Base):
    """A cross-tenant event surfaced to kavachio_admin users — something a broker
    did that Kavachio staff need to know about (today: a Bordereau Setup being
    activated).

    Distinct from ActivityEvent on purpose: activity_events is a per-tenant AUDIT
    trail (append-only, never presented as work), whereas this is a PRODUCT feed
    read cross-tenant by platform admins, with unread state. Reusing the audit
    table for it would couple the two and make either hard to change.

    Deliberately generic — `kind` is a free string and `details` an arbitrary
    JSON payload, so a new notifiable event needs no schema change, only a new
    `kind` at the call site. Written by ``notifications.notify_platform_admins``.
    Requires migration 18_platform_notifications.sql on RLS/prod (dev
    auto-creates via ``Base.metadata.create_all``).

    Read state lives on the reader, not here: AppUser.notifications_seen_at is a
    per-admin watermark (rows newer than it are unread), since the only product
    action is "mark what I've seen as read".
    """
    __tablename__ = "platform_notification"
    id = Column(Integer, primary_key=True)
    kind = Column(String, index=True, nullable=False)      # e.g. bordereau_setup_activated
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id
    actor = Column(String, nullable=True)                  # email of the acting user
    title = Column(String, nullable=False)                 # one-line headline
    body = Column(Text, nullable=True)                     # optional supporting sentence
    target = Column(String, nullable=True)                 # e.g. "pipeline:123"
    details = Column(JSON, nullable=True)                  # kind-specific payload
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
    # HOW each output column got its source — method, confidence, status and the
    # runners-up. `column_mapping` says what won; this says why, and it also
    # records the fields deliberately left unmapped because nothing cleared the
    # confidence bar. Without it a mapping is an assertion nobody can check.
    # {output_sheet: [semantic_mapping.Decision.to_dict(), ...]}
    mapping_decisions = _payload(Column(JSON, nullable=True))
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
    data = _payload(Column(JSON, nullable=False))
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


class Pipeline(Base):
    """The complete processing configuration for one (carrier, program): one
    Input Template (a DirectFormat — the learned input layout), one Output
    Template (an ExportTemplate), and 1..N contracts (via PipelineContract).

    The ACTIVE pipeline for a (carrier, program) is what ``/direct/run`` resolves
    and executes against — replacing the old ``DirectFormat.approved`` + per-
    contract activation model. Only one pipeline is active per (carrier, program).
    Requires migration 10_pipeline.sql on RLS/prod (dev auto-creates via
    ``Base.metadata.create_all``)."""
    __tablename__ = "pipeline"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)          # FK -> tenant.tenant_id
    name = Column(String, nullable=True)
    carrier_party_id = Column(Integer, index=True, nullable=True)   # FK -> party.party_id
    program_id = Column(Integer, index=True, nullable=True)         # FK -> program.program_id
    input_format_id = Column(Integer, index=True, nullable=True)    # FK -> direct_format.id
    output_template_id = Column(Integer, index=True, nullable=True)  # FK -> export_templates.id
    # Which broker this setup is for. NULL on every setup built before the
    # broker level existed — those stay (carrier, program) setups and keep
    # running exactly as they did, so this is purely additive.
    broker_party_id = Column(Integer, index=True, nullable=True)     # FK -> party.party_id
    # draft (never activated) | active (the one runs use) | superseded (replaced
    # by a newer active pipeline for the same carrier+program).
    status = Column(String, default="draft")
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PipelineContract(Base):
    """Which contract(s) govern a pipeline, optionally pinned to one output
    sheet. ``sheet_key`` NULL = the fallback contract (governs any output sheet
    not pinned to another contract). Mirrors the old
    ``DirectFormat.sheet_contracts`` + ``contract_id`` fallback semantics.
    ``tenant_id`` is denormalised so RLS (08_rls_policies.sql, which only
    isolates tables carrying a tenant_id) covers this join table too."""
    __tablename__ = "pipeline_contract"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)   # FK -> tenant.tenant_id
    pipeline_id = Column(Integer, index=True, nullable=False)  # FK -> pipeline.id
    contract_id = Column(Integer, nullable=False)           # FK -> contract.contract_id
    sheet_key = Column(String, nullable=True)               # output sheet; NULL = fallback
    position = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class MissingBdxColumn(Base):
    """One column the CONTRACT expects a bordereau to report that the setup's
    sample BDX does not carry — the "NOTE" shown on a setup after it is built.

    Written by ``missing_columns.analyze_pipeline`` (one quick model call over the
    ALREADY-extracted contract clauses/rules; the contract PDF is never re-read)
    and replaced wholesale on every re-analysis, so a pipeline's rows are always
    one consistent snapshot and re-running can never duplicate them.

    ``status``:
      ``missing`` — a real gap; ``column_name`` names it.
      ``clean``   — the single marker row written when an analysis COMPLETED and
                    found nothing (``column_name`` ''). It is what separates
                    "checked, all good" from "never checked", so revisiting a
                    setup with no gaps doesn't re-run the model every time.

    Requires migration 18_missing_bdx_columns.sql on RLS/prod (dev auto-creates
    via ``Base.metadata.create_all``)."""
    __tablename__ = "missing_bdx_columns"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)     # FK -> tenant.tenant_id
    pipeline_id = Column(Integer, index=True, nullable=False)  # FK -> pipeline.id
    format_id = Column(Integer, index=True, nullable=True)     # FK -> direct_format.id
    contract_id = Column(Integer, index=True, nullable=True)   # FK -> contract.contract_id
    # The BDX/output sheet the column belongs to. '' (never NULL) when it applies
    # to the whole bordereau — NULLs are distinct in Postgres, which would defeat
    # the uniqueness guard below.
    sheet_key = Column(String, nullable=False, default="")
    status = Column(String, nullable=False, default="missing")  # missing | clean
    column_name = Column(String, nullable=False, default="")
    # Case/space-insensitive form of column_name — the dedup key.
    normalized_name = Column(String, nullable=False, default="")
    severity = Column(String, nullable=True)          # required | recommended
    reason = Column(Text, nullable=True)              # why the contract needs it
    contract_reference = Column(Text, nullable=True)  # verbatim clause words
    # Where those words came from. Both are resolved by matching the quote back
    # to the clause row it was taken from — never taken from the model — so a
    # page shown beside a quote is one the contract really carries. NULL when the
    # quote could not be traced, and the UI then shows the quote with no page.
    source_page = Column(Integer, nullable=True)
    clause_label = Column(String, nullable=True)      # clause title / rule name
    related_output_field = Column(String, nullable=True)
    confidence = Column(Float, nullable=True)
    analyzed_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        # Re-analysis replaces rows, but this makes a duplicate impossible even
        # if two analyses race for the same pipeline.
        UniqueConstraint("pipeline_id", "sheet_key", "normalized_name",
                         name="uq_missing_bdx_column"),
        # The read path is always "this pipeline's rows" (the setup screen) —
        # composite so the status filter is served from the index too.
        Index("ix_missing_bdx_columns_pipeline_status", "pipeline_id", "status"),
        Index("ix_missing_bdx_columns_tenant_pipeline", "tenant_id", "pipeline_id"),
    )


class AiResponseCache(Base):
    """Memo of a model answer, keyed by a hash of the EXACT input that produced it.

    Several calls in the upload pipeline ask a question whose answer does not
    depend on the file being uploaded at all:

      generic_bind   — binding Kavachio's generic rule library to an output
                       template's columns. Depends only on (library rows,
                       template fields), so every contract uploaded against the
                       same template re-bought the identical answer. It is also
                       the expensive one: on a measured run the library was 50 of
                       the 80 Call-3 items, and Call 3 is ~59% of the bill.
      formula_infer  — which output-template columns are arithmetically computed.
                       Depends only on the column catalog.
      var_topup      — alternate real-world spellings for an enum rule's values.
                       Depends only on (field, values).

    A hit returns the SAME payload the model returned, so downstream behaviour is
    identical either way — every call runs at temperature 0 with a fixed seed, so
    a cached answer IS what a re-ask returns.

    `cache_key` is a SHA-256 over every input that can change the answer, so a
    template edit, a rule-library edit or a catalog bump all miss rather than
    serving something stale.

    Fail-open by design (see ai_cache.py): any error reading or writing this table
    is swallowed and the caller makes the model call.

    Requires migration 19_ai_response_cache.sql on RLS/prod (dev auto-creates via
    ``Base.metadata.create_all``)."""
    __tablename__ = "ai_response_cache"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)  # FK -> tenant.tenant_id
    # Which question this answers. Namespaces the key space so two callers can
    # never collide even if their inputs happened to hash alike.
    kind = Column(String, nullable=False)
    cache_key = Column(String, nullable=False)   # sha256 hex, see ai_cache.make_key
    payload = Column(JSON, nullable=False)       # the answer, verbatim
    hits = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("kind", "cache_key", name="uq_ai_response_cache"),
        Index("ix_ai_response_cache_tenant_kind", "tenant_id", "kind"),
    )


def exception_severity_counts(exceptions) -> tuple:
    """(critical, warning, info) totals for an OutputExport exceptions list.
    Severity aliases mirror the dashboard's normalization (_norm_sev)."""
    if isinstance(exceptions, str):
        try:
            exceptions = json.loads(exceptions)
        except Exception:
            exceptions = None
    crit = warn = info = 0
    for item in exceptions if isinstance(exceptions, list) else []:
        sv = (item.get("severity") or "").lower() if isinstance(item, dict) else ""
        if sv in ("critical", "crit", "error", "fatal", "high"):
            crit += 1
        elif sv in ("warning", "warn", "medium", "med"):
            warn += 1
        else:
            info += 1
    return crit, warn, info


def _backfill_severity_counts(batch: int = 200) -> None:
    """One-time backfill of the per-severity totals for output_exports rows
    created before the columns existed. Batched so the multi-MB exceptions
    blobs are never all in memory at once; no-op once every row is filled."""
    with SessionLocal() as s:
        while True:
            rows = (s.query(OutputExport.id, OutputExport.exceptions)
                    .filter(OutputExport.critical_count.is_(None))
                    .limit(batch).all())
            if not rows:
                return
            for rid, ex in rows:
                crit, warn, info = exception_severity_counts(ex)
                s.query(OutputExport).filter(OutputExport.id == rid).update(
                    {"critical_count": crit, "warning_count": warn,
                     "info_count": info})
            s.commit()


class SubmissionSchedule(Base):
    """Group 3 (C-1/C-5/C-6/C-8) — one row per program: how often this broker owes
    a BDX to the carrier, and when each one is due. The calendar is derived from
    the contract by default (Program.bdx_frequency + contract.inception_dt); the
    *_override columns let ops set/correct it by hand (C-6). When neither the
    contract nor an override resolves a frequency + anchor, no calendar is built
    and the UI shows a 'set it up' prompt instead of guessed deadlines.
    """
    __tablename__ = "submission_schedule"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    program_id = Column(Integer, index=True, nullable=False)   # FK -> program.program_id
    contract_id = Column(Integer, index=True, nullable=True)   # anchor source (inception_dt)
    # Manual overrides — win over the contract-derived values when set (C-6/C-8).
    frequency_override = Column(String, nullable=True)         # 'weekly'|'monthly'|'quarterly'
    anchor_date_override = Column(Date, nullable=True)         # start point for period 0
    # Tuning knobs (sensible defaults; adjustable per program).
    # Monthly/quarterly deadline: "due on the 10th" of the month after the
    # period. NULL falls back to due_offset_days so pre-existing schedules keep
    # resolving. See submission_calendar.due_date_for.
    due_day_of_month = Column(Integer, default=10)
    # Weekly only — weekly periods end on arbitrary dates, so a day-of-month
    # cannot express their deadline. Still the fallback for NULL day-of-month.
    due_offset_days = Column(Integer, default=10)             # due = period_end + this
    soon_window_days = Column(Integer, default=5)             # 'due soon' starts this early
    # DEPRECATED and no longer read by anything. A deadline is now either met or
    # missed — the day after the due date the bordereau is overdue, with no grace.
    # Kept only so existing rows load without a migration; do not reintroduce.
    grace_days = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("program_id", name="uq_submission_schedule_program"),)


class ExpectedSubmission(Base):
    """Group 3 — one generated calendar row: a period this broker is expected to
    submit a BDX for, and its computed due date. `status` is recomputed from the
    due date + the warning window and whether a file was received for the period.
    """
    __tablename__ = "expected_submission"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    program_id = Column(Integer, index=True, nullable=False)
    # WHICH BROKER owes this one. A programme is reported by several brokers and
    # each owes its own file, so "Corvin Risk is nine days late" is a statement
    # about one row, not about the programme. NULL means the obligation is not
    # attributed to a broker — either the programme has no broker on it yet, or
    # the row predates this column. A NULL row is adopted by the first broker
    # put on the programme rather than left orphaned, so its arrival history
    # survives (see materialize_schedule).
    broker_party_id = Column(Integer, index=True, nullable=True)
    schedule_id = Column(Integer, index=True, nullable=True)   # FK -> submission_schedule.id
    period = Column(String, nullable=False)                    # '2026-01' | '2026-Q1' | '2026-W03'
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False, index=True)
    # scheduled | due_soon | due_today | overdue | on_time | received_late
    # ('late' is retired — it only ever meant "overdue past the grace period",
    #  and there is no grace period any more. Stored rows are migrated below.)
    status = Column(String, default="scheduled", index=True)
    # The FIRST arrival for this period, and the only one that decides on_time
    # vs received_late. A correction sent three weeks later must not repaint a
    # missed deadline green, so these two stay pinned to version 1 and the later
    # versions are counted separately below.
    received_at = Column(Date, nullable=True)
    received_export_id = Column(Integer, nullable=True)        # FK -> output_exports.id
    # --- versions and release (see SubmissionVersion) -----------------------
    # How many files have been submitted for this period. 1 = the original only;
    # 2+ means it was corrected and resent. Denormalised from submission_version
    # so the calendar can render a version badge without a per-row query.
    version_count = Column(Integer, default=0)
    # The most recent arrival, whichever version it was. `received_at` answers
    # "did they meet the deadline"; this answers "what are we looking at now".
    latest_received_at = Column(Date, nullable=True)
    # SENT ONWARD. Producing a file and releasing it to its recipient are two
    # different acts — a bordereau can be generated on the 3rd and only sent to
    # the reinsurer on the 6th — so the calendar records them separately and can
    # answer "did Munich Re actually get July, and when?".
    released_at = Column(Date, nullable=True)
    released_count = Column(Integer, default=0)
    # CHASING. Recorded so the screen can say "chased 2 days ago" instead of
    # offering a button whose effect nobody can see, and so a second chase is a
    # deliberate act rather than an accident.
    chased_at = Column(Date, nullable=True)
    chase_count = Column(Integer, default=0)
    # The overdue bell fired for this row. Separate from `status` (which is
    # recomputed on read) so the reminder fires exactly once and survives a
    # re-materialize — status is a bad idempotency guard.
    overdue_notified = Column(Boolean, default=False)
    # The "due soon" bell fired for this row (once, when it entered the warning
    # window ahead of the due date) — the reminder BEFORE the deadline.
    due_soon_notified = Column(Boolean, default=False)
    # The "due today" bell fired for this row — the reminder ON the deadline.
    # Its own flag because the due date is its own moment: due_soon may have
    # rung days earlier, and overdue has not happened yet.
    due_today_notified = Column(Boolean, default=False)
    # C-9 email — DELIBERATELY separate from the *_notified bell flags. sweep_overdue
    # runs from three places, one of which is a calendar PAGE VIEW; sharing a flag
    # would either mail on every page view, or let a lazy sweep consume the bell and
    # leave the row permanently un-emailed. Two flags make the channels independent:
    # whichever sweep rings the bell, the next SCHEDULED run still mails it.
    overdue_emailed = Column(Boolean, default=False)
    due_soon_emailed = Column(Boolean, default=False)
    due_today_emailed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        # One obligation per (programme, broker, period). The broker is part of
        # the key because three brokers on one programme each owe July, and
        # NULL is the fourth, unattributed case.
        UniqueConstraint("program_id", "broker_party_id", "period",
                         name="uq_expected_submission_period"),
    )


class SubmissionVersion(Base):
    """One file submitted for one expected period — and, if it went on, the
    record of it being sent.

    THE POINT OF THIS TABLE. A period can be filed more than once: the original
    goes in on the due date, then something is found wrong and the period is
    sent again. Overwriting the first submission would quietly rewrite history —
    anyone looking back would see numbers that never actually went out — so each
    submission is kept as its own immutable row and the period carries a chain
    of them.

    `version_no` counts from 1 within a period. Version 1 is the `original`;
    every later one is a `corrected`. That distinction is the whole reason the
    kind is stored rather than derived at read time: a period that was filed
    once and a period that was filed three times are different facts about a
    broker, and the second must stay visible after the fact.

    A LATE file still belongs to its own period, not to the period it arrived
    in — which is what `expected_id` fixes. The arrival date lives here; the
    period it satisfies is decided by the calendar, not by the calendar month
    the file happened to turn up in.
    """
    __tablename__ = "submission_version"
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer, index=True, nullable=True)
    expected_id = Column(Integer, index=True, nullable=False)  # FK -> expected_submission.id
    program_id = Column(Integer, index=True, nullable=True)
    broker_party_id = Column(Integer, index=True, nullable=True)
    period = Column(String, nullable=True)          # denormalised for readable history
    version_no = Column(Integer, nullable=False, default=1)
    # original | corrected. Derived from version_no when the row is written, and
    # then kept — see the class docstring.
    kind = Column(String, default="original")
    # --- the file arriving --------------------------------------------------
    received_at = Column(Date, nullable=True)
    received_export_id = Column(Integer, nullable=True)   # FK -> output_exports.id
    source_filename = Column(String, nullable=True)
    # Where the period came from: 'explicit' (the caller said so), 'filename'
    # (read off the file name), or 'oldest_open' (the fallback guess). Recorded
    # because "we assumed this was July" and "the file said July" are different
    # levels of confidence and an operator checking a wrong month needs to know
    # which one this was.
    period_source = Column(String, nullable=True)
    # --- the file going onward ---------------------------------------------
    released_at = Column(Date, nullable=True)
    released_to = Column(String, nullable=True)     # reinsurer / syndicate / regulator / finance
    released_by = Column(String, nullable=True)     # actor email
    release_ref = Column(String, nullable=True)     # their reference, if they give one
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    modified_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("expected_id", "version_no",
                         name="uq_submission_version_no"),
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


def _sqlite_relax_expected_submission_key(conn) -> bool:
    """Drop a legacy UNIQUE(program_id, period) on expected_submission, SQLite-style.

    The obligation key gained the broker, so that old uniqueness now BLOCKS the
    fan-out: two brokers cannot both owe 2026-07 under it. Postgres can simply
    drop the constraint; SQLite cannot drop one declared inside CREATE TABLE, so
    the only way out is the standard rebuild — make the new table, copy the rows
    across, swap the names.

    Detection looks at unique CONSTRAINTS as well as indexes: SQLAlchemy's SQLite
    dialect reports a table-level UNIQUE as a constraint and hides its
    `sqlite_autoindex_*` from get_indexes() entirely, so checking indexes alone
    finds nothing and the rebuild silently never runs.

    Returns True when the table was rebuilt, so the caller knows to reflect it
    again — every cached column list for this table is stale afterwards.
    """
    try:
        insp = inspect(conn)
        if not insp.has_table("expected_submission"):
            return False
        target = {"program_id", "period"}
        legacy = [uc for uc in insp.get_unique_constraints("expected_submission")
                  if set(uc.get("column_names") or []) == target]
        legacy += [ix for ix in insp.get_indexes("expected_submission")
                   if ix.get("unique") and set(ix.get("column_names") or []) == target]
        if not legacy:
            return False
        cols = [c["name"] for c in insp.get_columns("expected_submission")]
        col_list = ", ".join(cols)
        conn.exec_driver_sql("ALTER TABLE expected_submission "
                             "RENAME TO expected_submission__old")
        ExpectedSubmission.__table__.create(bind=conn)
        # Only the columns the OLD table had — the new ones are left at their
        # defaults and filled in by the backfill that runs after this.
        conn.exec_driver_sql(
            f"INSERT INTO expected_submission ({col_list}) "
            f"SELECT {col_list} FROM expected_submission__old")
        conn.exec_driver_sql("DROP TABLE expected_submission__old")
        return True
    except Exception:
        # A failed rebuild must not take the whole boot down. Put the original
        # table back and carry on: the calendar is then limited to one broker
        # per period on this database, which is exactly how it behaved before —
        # degraded, not broken.
        try:
            conn.exec_driver_sql("DROP TABLE IF EXISTS expected_submission")
            conn.exec_driver_sql("ALTER TABLE expected_submission__old "
                                 "RENAME TO expected_submission")
        except Exception:
            pass
        return False


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
        # FIRST, before any column is added: the calendar's obligation key gained
        # the broker, and the old UNIQUE(program_id, period) would block the
        # fan-out. On SQLite that means rebuilding the table, so it has to happen
        # while the column list is still the one on disk.
        try:
            if dialect == "postgresql":
                conn.exec_driver_sql(
                    "ALTER TABLE expected_submission "
                    "DROP CONSTRAINT IF EXISTS uq_expected_submission_program_period")
            else:
                conn.exec_driver_sql(
                    "DROP INDEX IF EXISTS uq_expected_submission_program_period")
        except Exception:
            pass
        if dialect == "sqlite" and _sqlite_relax_expected_submission_key(conn):
            inspector = inspect(conn)     # rebuilt — every cached shape is stale
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
        _ensure_column(conn, inspector, "export_templates", "output_format", "VARCHAR DEFAULT 'xlsx'")
        # Output BDX template scoped at carrier + programme + broker + contract.
        # Nullable with no backfill: an existing template keeps resolving at
        # (carrier, program) scope, so no existing setup changes behaviour.
        _ensure_column(conn, inspector, "export_templates", "program_id", "INTEGER")
        _ensure_column(conn, inspector, "export_templates", "broker_party_id", "INTEGER")
        _ensure_column(conn, inspector, "export_templates", "source_kind", "VARCHAR")
        _ensure_column(conn, inspector, "export_templates", "standard_meta", json_type)
        # The Bordereau Setup's broker level (NULL = a pre-broker setup).
        _ensure_column(conn, inspector, "pipeline", "broker_party_id", "INTEGER")
        # Generated-output metadata (plan section 22). template_version is the
        # one that matters: it keeps a historical download pinned to the layout
        # it was actually written with.
        _ensure_column(conn, inspector, "output_exports", "template_version", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "pipeline_id", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "carrier_party_id", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "program_id", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "broker_party_id", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "contract_id", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "output_format", "VARCHAR")
        _ensure_column(conn, inspector, "output_exports", "sample_comparison", json_type)
        _ensure_column(conn, inspector, "tenant", "onboarding_skipped", "BOOLEAN DEFAULT FALSE")
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
        # The documents a contract is made of. `contract_document` already
        # existed in the canonical schema, so create_all() will NOT touch it —
        # the five columns the contract flow needs have to be ALTERed in. See
        # migrations/14_contract_documents.sql for what each one is for.
        _ensure_column(conn, inspector, "contract_document",
                       "contract_document_satisfies_reference", "VARCHAR")
        _ensure_column(conn, inspector, "contract_document",
                       "contract_document_effective_from", "DATE")
        _ensure_column(conn, inspector, "contract_document",
                       "contract_document_is_active", "BOOLEAN DEFAULT TRUE")
        _ensure_column(conn, inspector, "contract_document",
                       "contract_document_extracted", json_type)
        _ensure_column(conn, inspector, "contract_document",
                       "contract_document_blob", blob_type)
        # The broker's counter-proposal on a contract under negotiation. See
        # migrations/15_contract_negotiation.sql for why a note is not enough.
        _ensure_column(conn, inspector, "contract_approval",
                       "approval_proposed_changes", json_type)
        # The authored contract: its commercial terms and its wording sections.
        # See migrations/16_contract_authoring.sql.
        _ensure_column(conn, inspector, "contract", "commercial_terms", json_type)
        _ensure_column(conn, inspector, "contract", "wording_sections", json_type)
        # Direct-lane setup scoping (carrier + program)
        _ensure_column(conn, inspector, "direct_format", "carrier_party_id", "INTEGER")
        _ensure_column(conn, inspector, "direct_format", "program_id", "INTEGER")
        # Mapping traceability. NULL on every format built before it existed —
        # their mapping still runs, it simply has no recorded reasoning.
        _ensure_column(conn, inspector, "direct_format", "mapping_decisions", json_type)
        # Group 3: added to expected_submission after the table already existed, so
        # create_all won't add it — ALTER it in (default false backfills old rows).
        _ensure_column(conn, inspector, "expected_submission", "overdue_notified",
                       "BOOLEAN DEFAULT FALSE")
        # C-9: the "due soon" reminder flag (fires once before the due date).
        _ensure_column(conn, inspector, "expected_submission", "due_soon_notified",
                       "BOOLEAN DEFAULT FALSE")
        # The "due today" reminder flag — the third deadline moment, added when
        # grace was removed and the due date became a state of its own.
        _ensure_column(conn, inspector, "expected_submission", "due_today_notified",
                       "BOOLEAN DEFAULT FALSE")
        # C-9 email delivery flags (see ExpectedSubmission for why they are not
        # the *_notified flags). Default FALSE → the first scheduled sweep after
        # this ships mails the EXISTING backlog; see migration 20 to opt out.
        _ensure_column(conn, inspector, "expected_submission", "overdue_emailed",
                       "BOOLEAN DEFAULT FALSE")
        _ensure_column(conn, inspector, "expected_submission", "due_soon_emailed",
                       "BOOLEAN DEFAULT FALSE")
        _ensure_column(conn, inspector, "expected_submission", "due_today_emailed",
                       "BOOLEAN DEFAULT FALSE")
        # Requirement 17.2 — "due, released and corrected versions".
        #
        # broker_party_id turns one obligation per programme into one per
        # (programme x broker), which is what the calendar screen actually
        # shows. NULL on every pre-existing row; materialize_schedule adopts
        # those into the programme's first broker rather than orphaning them,
        # so no arrival history is lost.
        _ensure_column(conn, inspector, "expected_submission", "broker_party_id",
                       "INTEGER")
        # Roll-ups denormalised from submission_version so the calendar renders
        # a version badge and a "sent onward" date without a query per row.
        _ensure_column(conn, inspector, "expected_submission", "version_count",
                       "INTEGER DEFAULT 0")
        _ensure_column(conn, inspector, "expected_submission", "latest_received_at",
                       "DATE")
        _ensure_column(conn, inspector, "expected_submission", "released_at", "DATE")
        _ensure_column(conn, inspector, "expected_submission", "released_count",
                       "INTEGER DEFAULT 0")
        _ensure_column(conn, inspector, "expected_submission", "chased_at", "DATE")
        _ensure_column(conn, inspector, "expected_submission", "chase_count",
                       "INTEGER DEFAULT 0")
        # Backfill the roll-ups for rows that arrived before versions existed:
        # a period already marked received has exactly one submission behind it.
        try:
            conn.exec_driver_sql(
                "UPDATE expected_submission "
                "SET version_count = 1, latest_received_at = received_at "
                "WHERE received_at IS NOT NULL "
                "AND (version_count IS NULL OR version_count = 0)")
            conn.exec_driver_sql(
                "UPDATE expected_submission SET version_count = 0 "
                "WHERE received_at IS NULL AND version_count IS NULL")
            conn.exec_driver_sql(
                "UPDATE expected_submission SET released_count = 0 "
                "WHERE released_count IS NULL")
        except Exception:
            pass    # table not there yet; create_all built it with no rows
        # The version rows those backfilled counts describe. Written here rather
        # than left implied, so "show me every file sent for July" returns the
        # original too and not just the corrections that came after it.
        try:
            conn.exec_driver_sql(
                "INSERT INTO submission_version "
                "(tenant_id, expected_id, program_id, broker_party_id, period, "
                " version_no, kind, received_at, received_export_id, period_source) "
                "SELECT e.tenant_id, e.id, e.program_id, e.broker_party_id, e.period, "
                "       1, 'original', e.received_at, e.received_export_id, 'backfill' "
                "FROM expected_submission e "
                "WHERE e.received_at IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM submission_version v "
                "                WHERE v.expected_id = e.id)")
        except Exception:
            pass    # submission_version not created yet, or already backfilled
        # 'late' is retired: with no grace period it and 'overdue' mean the same
        # thing. Fold the stored rows over, so the dashboard counts (which read
        # the column rather than recomputing) do not lose them. Idempotent — the
        # second run matches nothing.
        try:
            conn.exec_driver_sql(
                "UPDATE expected_submission SET status = 'overdue' "
                "WHERE status = 'late'")
        except Exception:
            pass    # table not there yet; create_all built it with no rows
        # Deadlines are stated as a day of the month now, not an offset from the
        # period end. See submission_calendar.due_date_for.
        _ensure_column(conn, inspector, "submission_schedule", "due_day_of_month",
                       "INTEGER")
        # Backfill from the offset it replaces. For monthly/quarterly the two are
        # the SAME DATE whenever the offset is <= 28 — periods end on a month's
        # last day, so "+10 days" already meant "the 10th of the next month".
        # Restricting to <= 28 is what makes that exact: a larger offset could
        # have crossed into the month after, so those are left NULL and keep
        # resolving through the offset path rather than being silently moved.
        try:
            conn.exec_driver_sql(
                "UPDATE submission_schedule SET due_day_of_month = due_offset_days "
                "WHERE due_day_of_month IS NULL AND due_offset_days IS NOT NULL "
                "AND due_offset_days BETWEEN 1 AND 28")
        except Exception:
            pass
        _ensure_column(conn, inspector, "landing_record", "output_export_id", "INTEGER")
        _ensure_column(conn, inspector, "app_user", "last_login_at", "TIMESTAMP")
        _ensure_column(conn, inspector, "app_user", "reset_token", "VARCHAR")
        _ensure_column(conn, inspector, "app_user", "reset_token_expires", "TIMESTAMPTZ")
        # Per-admin read watermark for the platform_notification feed.
        _ensure_column(conn, inspector, "app_user", "notifications_seen_at", "TIMESTAMP")
        # Denormalized per-severity exception totals (dashboard donut)
        _ensure_column(conn, inspector, "output_exports", "critical_count", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "warning_count", "INTEGER")
        _ensure_column(conn, inspector, "output_exports", "info_count", "INTEGER")
        # Which clause a missing-column finding was quoted from (added after the
        # table shipped; create_all only CREATEs, it never ALTERs).
        _ensure_column(conn, inspector, "missing_bdx_columns", "clause_label", "VARCHAR")

        # v4 model: tables shared between the ops ORM and the canonical schema
        # (tenant, party, program, contract, app_user, upload, …) are created
        # by Base.metadata first, so canonical create_all skips them and any
        # canonical column the ORM doesn't map — the lineage/SCD-2 block, the
        # business columns only the ingester writes — would be missing on a
        # fresh DB. ALTER every missing canonical column in (idempotent).
        from canonical import CANONICAL_TABLES as _CANON
        for _t_name, _t in _CANON.items():
            if not inspector.has_table(_t_name):
                continue                      # canonical create_all will build it
            for _col in _t.c:
                try:
                    _ddl = _col.type.compile(dialect=engine.dialect)
                except Exception:  # noqa: BLE001 — fall back to a safe type
                    _ddl = "VARCHAR"
                _ensure_column(conn, inspector, _t_name, _col.name, _ddl)

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

    # One-time, idempotent backfill: give every currently-approved DirectFormat
    # ("setup") a matching active Pipeline so /direct/run keeps resolving the
    # same config after it switches to pipeline-based resolution. Best-effort —
    # never blocks boot. (Prod/RLS returns early above and runs
    # scripts/backfill_pipelines.py instead, after 10_pipeline.sql.)
    try:
        with SessionLocal() as _s:
            backfill_pipelines(_s)
    except Exception as e:  # noqa: BLE001 — best-effort, retried next boot
        import logging
        logging.getLogger("bdx.db").warning("Pipeline backfill skipped: %s", e)

    # One-time, idempotent backfill of the per-severity exception totals for
    # rows that predate the critical/warning/info_count columns. Best-effort.
    try:
        _backfill_severity_counts()
    except Exception as e:  # noqa: BLE001 — best-effort, retried next boot
        import logging
        logging.getLogger("bdx.db").warning("Severity-count backfill skipped: %s", e)
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


def backfill_pipelines(session) -> int:
    """Create one active Pipeline for each approved DirectFormat that doesn't
    have one yet, mirroring its output template + contract bindings. Idempotent:
    guards on an existing Pipeline with the same input_format_id, so re-runs are
    safe. Returns the number of pipelines created.

    Used both at dev boot (init_db) and by scripts/backfill_pipelines.py in prod.
    """
    created = 0
    formats = (session.query(DirectFormat)
               .filter(DirectFormat.approved == 1,
                       DirectFormat.output_template_id.isnot(None))
               .all())
    for df in formats:
        exists = (session.query(Pipeline)
                  .filter(Pipeline.input_format_id == df.id)
                  .first())
        if exists:
            continue
        pipe = Pipeline(
            tenant_id=df.tenant_id, name=df.name,
            carrier_party_id=df.carrier_party_id, program_id=df.program_id,
            input_format_id=df.id, output_template_id=df.output_template_id,
            status="active")
        session.add(pipe)
        session.flush()  # need pipe.id for the contract rows

        # Mirror the governing set exactly: per-sheet pins from sheet_contracts,
        # plus the single fallback contract_id (unless already pinned).
        pinned: set[int] = set()
        pos = 0
        for sheet, cid in (df.sheet_contracts or {}).items():
            if not cid:
                continue
            cid = int(cid)
            session.add(PipelineContract(
                tenant_id=df.tenant_id, pipeline_id=pipe.id, contract_id=cid,
                sheet_key=str(sheet), position=pos))
            pinned.add(cid)
            pos += 1
        if df.contract_id and int(df.contract_id) not in pinned:
            session.add(PipelineContract(
                tenant_id=df.tenant_id, pipeline_id=pipe.id,
                contract_id=int(df.contract_id), sheet_key=None, position=pos))
        created += 1
    session.commit()
    return created
