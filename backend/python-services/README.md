# BDX Mapper — Backend

Python/FastAPI service that onboards an MGA's Bordereaux (BDX) Excel format,
auto-maps its columns to a canonical premium data model using **embeddings +
Gemini LLM**, persists the resulting mapping as a **glom spec**, and ingests
subsequent BDX files into a Data Warehouse (DWH) by replaying that spec.
 
---

## How it works

### 1. Onboarding — sample BDX → saved mapper

```
sample.xlsx + mga ─► /mapper/generate ─► saved Mapper + categorized proposal
                                              │
                                              ▼
                            MGA reviews "likely" / "unsuccessful"
                                              │
                                              ▼
                              PUT /mapper/{id}  (corrected spec)
```

1. **Read** the uploaded Excel with `pandas` (sheet/skip-rows configurable).
2. **Header-only mapping.** Sample row values are returned for display ONLY —
   they are **not** used to drive the mapping. The system maps source
   **column headers → canonical fields**, nothing else.
3. **Embed** every header and every canonical-field description with
   `sentence-transformers/all-MiniLM-L6-v2`. Cosine similarity gives the
   **top-5 canonical candidates** per source header.
4. **Finalize** with **Gemini** (`gemini-2.5-flash` via `google-genai`): the
   LLM receives the canonical data model, the source headers, and the
   embedding candidates, and returns a conservative
   `{canonical_field: source_header | null}` mapping. It is told to use any
   header at most once and to return `null` when unsure.
5. **Fallback**: if no `GEMINI_API_KEY` is configured, a greedy top-1 cosine
   assignment is used (threshold 0.35).
6. **Categorize each SOURCE COLUMN** by its mapping score. Buckets are
   indexed by the source header, so the total number of entries equals the
   number of source columns in the file.

   | Bucket          | Score range          | Meaning                                   |
   | --------------- | -------------------- | ----------------------------------------- |
   | `successful`    | `>= 0.65`            | High-confidence binding, ready to use.    |
   | `likely`        | `0.45 – 0.65`        | Weak binding — MGA should review.         |
   | `unsuccessful`  | `< 0.45` or no bind  | MGA must correct. Includes top-3 `suggestions`. |

   `canonical_unmapped` separately lists canonical fields the spec didn't fill,
   so the MGA can wire any leftover source manually.

7. **Auto-save.** The proposed spec is persisted as a `Mapper` row scoped to
   `(mga, carrier?, contract?)` with `approved=False`, and the response
   carries the new `mapper_id`.
8. **Correction loop.** The MGA fixes `likely` / `unsuccessful` entries and
   submits the corrected `spec` via `PUT /mapper/{id}` — optionally setting
   `approved=true` to lock it in.

The saved spec is a **glom spec**: a flat
`{canonical_field: source_column_name}` dict. `glom()` walks each row dict by
those keys at ingestion time.

### 2. Ingestion — actual BDX → DWH

```
new.xlsx ──► /bdx/upload ──► signature lookup ──► glom(spec) per row ──► dwh_bdx
```

1. Read the file, compute its **signature**.
2. Look up a saved `Mapper` for the given MGA whose `signature` matches.
3. **No match → 409** `no_matching_mapper`. The UI must prompt the MGA to
   create or update a mapping.
4. **Match →** apply `glom(row, spec)` for every row, normalize values
   (`NaN → None`, dates → ISO), and insert canonical records into `dwh_bdx`.
5. `/bdx/preview` is the same flow but returns the first 50 mapped rows
   without writing to the DWH.

### 3. Canonical data model — DB-aware

[`data_model.py`](data_model.py) targets the warehouse **Domain 3 (Policies &
Coverages)** and **Domain 4 (Premium & Money)** SQL schemas. Each canonical
field carries:

```python
"policy_number": {
    "table":       "policy",                    # destination DB table
    "column":      "policy_number",             # destination DB column
    "type":        "string",                    # logical type
    "description": "Policy number",
}
```

So a saved Mapper spec resolves end-to-end:

```
BDX header  ──spec──►  canonical name  ──DATA_MODEL──►  (table, column)
```

Tables covered (26 across Domains 1–4):

- **Domain 1 — Parties & Roles**: `tenant`, `party`, `party_address`,
  `party_contact`, `party_license`, `party_relationship`.
- **Domain 2 — Contracts & Programs**: `program`, `contract`, `contract_party`,
  `contract_terms`, `contract_amendment`, `field_requirement_rule`.
- **Domain 3 — Policies & Coverages**: `policy`, `coverage`, `layer`,
  `layer_participation`, `policy_attributes`, `insured_location`,
  `parametric_coverage_detail`.
- **Domain 4 — Premium & Money**: `upload`, `premium_transaction`,
  `commission`, `policy_fee`, `tax_or_surcharge`, `accruals_booked`,
  `fx_rate`.

Where a DB column is a foreign key to `party` (e.g. `policy.insured_party_id`,
`policy.writing_company_id`), the canonical field exposes the **human name**
(`insured_name`, `writing_company_name`) and the loader resolves it to the
party FK at insert time.

### 4. Glom mapping → DB-shaped JSON

`apply_spec` re-keys the saved `{canonical: source_column}` spec by
`(table, column)` and runs glom per row. Result is a nested record the loader
can hand directly to the DB:

```jsonc
// One mapped row from a BDX file
{
  "policy": {
    "policy_number":          "SSIC-GLN02-0000153-22",
    "policy_effective_dt":    "2022-09-25",
    "policy_expiration_dt":   "2023-09-25",
    "insured_party_id":       "1157 Alameda Apartments, LLC"   // loader → FK
  },
  "coverage": {
    "coverage_type":          "General Liability",
    "tiv":                    "5000000"
  },
  "premium_transaction": {
    "total_gross_premium":    "5287",
    "original_currency":      "USD"
  }
}
```

The Mapper row in SQLite holds the canonical-form spec; the per-table
re-keying happens at ingestion time so that the saved spec stays
table-structure-agnostic and survives schema migrations.

---

## Project layout

```
backend/
├── main.py          # FastAPI app & endpoints
├── mapper.py        # embedding + Gemini mapping generator, glom executor
├── data_model.py    # canonical premium data model (mirrors `coverage` table)
├── db.py            # SQLAlchemy models: Mapper, BDXRecord (SQLite)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set GEMINI_API_KEY
python main.py              # serves on http://0.0.0.0:8000
```

Storage is SQLite (`bdx.db` in the working directory), created on first run.

---

## API

| Method | Path                | Purpose                                                          |
| ------ | ------------------- | ---------------------------------------------------------------- |
| GET    | `/data-model`       | Return the canonical data model.                                 |
| POST   | `/mapper/generate`  | Multipart `mga` + `file` (xlsx), optional `carrier`, `contract`, `sheet_name`, `skip_rows`. Auto-saves an unapproved Mapper and returns categorized mapping (successful / likely / unsuccessful). |
| PUT    | `/mapper/{id}`      | JSON body `{spec, approved?, carrier?, contract?}`. MGA submits corrected mappings (and approves). |
| GET    | `/mapper`           | List mappers (filter by `?mga=`).                                |
| GET    | `/mapper/{id}`      | Get a single mapper.                                             |
| POST   | `/bdx/preview`      | Multipart `mga`, `file` + optional `sheet_name`, `skip_rows`. Returns up to 50 mapped rows. |
| POST   | `/bdx/upload`       | Multipart `mga`, `file` + optional `sheet_name`, `skip_rows`. Ingests rows into the DWH. |

**Optional Excel form fields** (apply to the three xlsx-accepting endpoints):
- `sheet_name` — sheet to read. Either a sheet name (e.g. `"Bordereau"`) or a
  zero-based index as a string (e.g. `"1"`). Defaults to the first sheet.
- `skip_rows` — integer; number of leading rows to skip before the header row.
  Useful when an MGA's file has title/blank rows above the table.
| GET    | `/dwh`              | Read mapped records from the DWH (filter by `?mga=`, `?limit=`). |

### `PUT /mapper/{id}` body
```json
{
  "spec": { "policy_number": "Policy Number", "policy_effective_date": "Policy Effective Date" },
  "approved": true,
  "carrier": null,
  "contract": null
}
```

### `/bdx/upload` mismatch response (HTTP 409)
```json
{
  "detail": {
    "error": "no_matching_mapper",
    "message": "File format does not match any saved mapper for this MGA.",
    "signature": ["..."]
  }
}
```

---

## Detailed process (end-to-end)

This section describes the intended MGA workflow using the Backend APIs.

### Step 0 — Auth (mock)
- **POST `/auth/login`**
  - Purpose: create/find a user and return a simple user/session object for the UI.
  - Implemented in `app_routes.py`.

### Step 1 — Configure tenant + directory
1. **GET/PUT `/tenants/{mga}`**
   - Purpose: store tenant configuration (legal name, tenant type, currency, internal codes).
2. **GET/POST/PUT `/parties` + `/parties/{party_id}`**
   - Purpose: manage party directory (insurers, brokers, insureds, etc.).
   - Parties can be scoped to `tenant` or `global`.
3. **(Optional) Party contacts**
   - **GET/POST `/parties/{party_id}/contacts`**
   - **DELETE `/parties/{party_id}/contacts/{contact_id}`**
4. **Program & contract setup**
   - **GET/POST/PUT `/programs` + `/programs/{program_id}`**
   - **POST `/programs/{program_id}/contracts`** (contract upload)
     - Purpose: upload a contract and store extracted metadata (currently stubbed AI extraction for UI flow).
   - **GET `/programs/{program_id}/contracts`**

### Step 2 — Extra (custom) fields
- **GET `/extra-fields`**
  - Purpose: list tenant-visible extra-field definitions (own + shared).
- **POST `/extra-fields`**
  - Purpose: create/update an extra-field definition for the tenant.
- **POST `/extra-fields/{key}/adopt`**
  - Purpose: adopt an existing shared definition into your tenant.

These extra-fields are surfaced in the mapping UI as “extra fields”.

### Step 3 — Upload a sample BDX → generate a mapper
1. (Optional) choose which sheets to inspect:
   - **POST `/bdx/sheets`**
   - Purpose: UI asks for sheet names and basic metadata.
2. Generate mapping from a sample Excel:
   - **POST `/mapper/generate`**
   - Purpose: generate a proposed header→canonical spec and auto-save it as a draft mapper.
   - Inputs:
     - `mga` (required)
     - `file` (required, Excel)
     - optional `carrier`, `contract`
     - optional `skip_rows`
     - optional `sheets` (comma-separated sheet names)

Backend behavior:
- Compute a signature from the workbook layout.
- If a previously seen signature exists, clone the spec and skip Gemini.
- Otherwise:
  - embed headers + canonical-field descriptions
  - rank top candidates
  - use Gemini to produce a conservative mapping
- Persist as `Mapper` with `approved=false`.
- Return:
  - `mapper_id`
  - `spec` / `spec_by_sheet`
  - candidate lists + confidence categories

### Step 4 — Review & approve the mapping (correction loop)
- **GET `/mapper/{mapper_id}`**
  - Purpose: load the mapper for the UI.
- **PUT `/mapper/{mapper_id}`**
  - Purpose: submit corrected mappings and optionally approve.
  - If `approved=true`, the mapper is marked approved and becomes eligible for ingestion matching.

### Step 5 — Preview / ingest real BDX data
1. Preview mapped rows:
   - **POST `/bdx/preview`**
   - Purpose: apply the saved mapper to the new file and return up to 50 mapped rows (does not write DWH).
2. Ingest:
   - **POST `/bdx/upload`**
   - Purpose: apply the saved mapper and write canonical records into the warehouse.
3. Inspect upload history:
   - **GET `/uploads`**
   - **GET `/uploads/{upload_id}`**
4. Read canonical output:
   - **GET `/dwh`**
   - Purpose: fetch reassembled canonical policies from the warehouse (filterable by `upload_id`, `limit`, `offset` depending on params).

Important behavior:
- In preview/upload, the backend computes a signature and finds a matching mapper for the MGA.
- If no mapper matches, backend returns HTTP **409** with:
  - `detail.error = "no_matching_mapper"`

### Step 6 — Exports (generate output BDX/workbooks)
1. Create/prepare an output template:
   - **POST `/export/template/generate`**
   - Purpose: upload a sample output workbook and draft an export template.
2. Update/approve the template:
   - **PUT `/export/template/{template_id}`**
3. Refresh AI mapping (retrofit):
   - **POST `/export/template/{template_id}/refresh`**
4. Generate an export workbook:
   - **POST `/export/generate`**
   - Purpose: produce an `.xlsx` from approved template + ingested canonical policies.

---

## Complete API reference (Python/FastAPI)

All endpoints are mounted by the FastAPI app in `main.py` and the router in `app_routes.py`.

### Mapper + BDX onboarding/ingestion (FastAPI app routes)
| Method | Path | Purpose |
|---|---|---|
| GET | `/data-model` | Return canonical data model used for mapping. |
| POST | `/bdx/sheets` | Inspect an uploaded workbook and list available sheet metadata. |
| POST | `/mapper/generate` | Generate proposed mapper from sample Excel and auto-save a draft mapper. |
| PUT | `/mapper/{mapper_id}` | Submit corrected mapping spec and optionally approve it. |
| GET | `/mapper` | List mappers (optionally filtered by `?mga=`). |
| GET | `/mapper/{mapper_id}` | Get a single mapper (used by UI mapping screen). |
| GET | `/mapper/{mapper_id}/file` | Download the original uploaded mapper sample workbook. |
| POST | `/bdx/preview` | Apply mapper spec to a new file and return up to 50 mapped rows. |
| POST | `/bdx/upload` | Apply mapper spec and ingest mapped rows into the canonical warehouse. |
| GET | `/uploads` | List ingestion uploads (optionally filtered by `?mga=`). |
| GET | `/uploads/{upload_id}` | Get upload metadata. |
| GET | `/dwh` | Fetch reassembled canonical policy output (optionally `upload_id`, `limit`, `offset`). |
| POST | `/export/template/generate` | Upload output sample and draft an export template. |
| PUT | `/export/template/{template_id}` | Update/approve an export template. |
| GET | `/export/template` | List templates (optional `?mga=`). |
| GET | `/export/template/{template_id}` | Get one template. |
| POST | `/export/template/{template_id}/refresh` | Refresh AI mapping inside a stored template. |
| POST | `/export/generate` | Generate an output xlsx from approved template + canonical policies. |

### Tenant, parties, programs, users, extra fields (router routes)
| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | Mock login; creates default user/tenant on first login. |
| GET | `/onboarding/status` | Four-step onboarding status summary for a tenant. |
| GET | `/tenants/{mga}` | Get tenant config. |
| PUT | `/tenants/{mga}` | Update tenant config. |
| GET | `/parties` | List parties (search `q`, filter `party_type`, `scope`). |
| POST | `/parties` | Create a party. |
| GET | `/parties/{party_id}` | Get party details. |
| PUT | `/parties/{party_id}` | Update party details. |
| GET | `/parties/{party_id}/contacts` | List contacts for a party. |
| POST | `/parties/{party_id}/contacts` | Create a party contact. |
| DELETE | `/parties/{party_id}/contacts/{contact_id}` | Delete a contact. |
| GET | `/programs` | List programs. |
| POST | `/programs` | Create program. |
| GET | `/programs/{program_id}` | Get program. |
| PUT | `/programs/{program_id}` | Update program. |
| POST | `/programs/{program_id}/contracts` | Upload contract; stores stubbed extracted metadata. |
| GET | `/programs/{program_id}/contracts` | List uploaded contracts for a program. |
| GET | `/dashboard/stats` | Dashboard stats for the home screen. |
| GET | `/activity` | List activity events (for activity feed). |
| GET | `/extra-fields` | List extra fields visible to tenant (own + shared). |
| POST | `/extra-fields` | Create/update an extra field definition. |
| POST | `/extra-fields/{key}/adopt` | Adopt a shared extra field into the tenant. |
| GET | `/users` | List users for the tenant. |
| POST | `/users` | Create a user. |
| PUT | `/users/{user_id}` | Update user details. |
| DELETE | `/users/{user_id}` | Delete a user. |

### File upload form fields (common)
Many xlsx upload endpoints accept:
- `mga` (required)
- `file` (required)
- optional `skip_rows`
- optional `sheet_name` (older single-sheet use; current UI primarily uses sheet picker + sheet filtering)
Additionally:
- `/mapper/generate` supports optional `sheets` (comma-separated sheet names)

---

## Detailed process (end-to-end)

This section describes the intended MGA workflow using the Backend APIs.

### Step 0 — Auth (mock)
- **POST `/auth/login`**
  - Purpose: create/find a user and return a simple user/session object for the UI.
  - Implemented in `app_routes.py`.

### Step 1 — Configure tenant + directory
1. **GET/PUT `/tenants/{mga}`**
   - Purpose: store tenant configuration (legal name, tenant type, currency, internal codes).
2. **GET/POST/PUT `/parties` + `/parties/{party_id}`**
   - Purpose: manage party directory (insurers, brokers, insureds, etc.).
   - Parties can be scoped to `tenant` or `global`.
3. **(Optional) Party contacts**
   - **GET/POST `/parties/{party_id}/contacts`**
   - **DELETE `/parties/{party_id}/contacts/{contact_id}`**
4. **Program & contract setup**
   - **GET/POST/PUT `/programs` + `/programs/{program_id}`**
   - **POST `/programs/{program_id}/contracts`** (contract upload)
     - Purpose: upload a contract and store extracted metadata (currently stubbed AI extraction for UI flow).
   - **GET `/programs/{program_id}/contracts`**

### Step 2 — Extra (custom) fields
- **GET `/extra-fields`**
  - Purpose: list tenant-visible extra-field definitions (own + shared).
- **POST `/extra-fields`**
  - Purpose: create/update an extra-field definition for the tenant.
- **POST `/extra-fields/{key}/adopt`**
  - Purpose: adopt an existing shared definition into your tenant.

These extra-fields are surfaced in the mapping UI as “extra fields”.

### Step 3 — Upload a sample BDX → generate a mapper
1. (Optional) choose which sheets to inspect:
   - **POST `/bdx/sheets`**
   - Purpose: UI asks for sheet names and basic metadata.
2. Generate mapping from a sample Excel:
   - **POST `/mapper/generate`**
   - Purpose: generate a proposed header→canonical spec and auto-save it as a draft mapper.
   - Inputs:
     - `mga` (required)
     - `file` (required, Excel)
     - optional `carrier`, `contract`
     - optional `skip_rows`
     - optional `sheets` (comma-separated sheet names)

Backend behavior:
- Compute a signature from the workbook layout.
- If a previously seen signature exists, clone the spec and skip Gemini.
- Otherwise:
  - embed headers + canonical-field descriptions
  - rank top candidates
  - use Gemini to produce a conservative mapping
- Persist as `Mapper` with `approved=false`.
- Return:
  - `mapper_id`
  - `spec` / `spec_by_sheet`
  - candidate lists + confidence categories

### Step 4 — Review & approve the mapping (correction loop)
- **GET `/mapper/{mapper_id}`**
  - Purpose: load the mapper for the UI.
- **PUT `/mapper/{mapper_id}`**
  - Purpose: submit corrected mappings and optionally approve.
  - If `approved=true`, the mapper is marked approved and becomes eligible for ingestion matching.

### Step 5 — Preview / ingest real BDX data
1. Preview mapped rows:
   - **POST `/bdx/preview`**
   - Purpose: apply the saved mapper to the new file and return up to 50 mapped rows (does not write DWH).
2. Ingest:
   - **POST `/bdx/upload`**
   - Purpose: apply the saved mapper and write canonical records into the warehouse.
3. Inspect upload history:
   - **GET `/uploads`**
   - **GET `/uploads/{upload_id}`**
4. Read canonical output:
   - **GET `/dwh`**
   - Purpose: fetch reassembled canonical policies from the warehouse (filterable by `upload_id`, `limit`, `offset` depending on params).

Important behavior:
- In preview/upload, the backend computes a signature and finds a matching mapper for the MGA.
- If no mapper matches, backend returns HTTP **409** with:
  - `detail.error = "no_matching_mapper"`

### Step 6 — Exports (generate output BDX/workbooks)
1. Create/prepare an output template:
   - **POST `/export/template/generate`**
   - Purpose: upload a sample output workbook and draft an export template.
2. Update/approve the template:
   - **PUT `/export/template/{template_id}`**
3. Refresh AI mapping (retrofit):
   - **POST `/export/template/{template_id}/refresh`**
4. Generate an export workbook:
   - **POST `/export/generate`**
   - Purpose: produce an `.xlsx` from approved template + ingested canonical policies.

---

## Complete API reference (Python/FastAPI)

All endpoints are mounted by the FastAPI app in `main.py` and the router in `app_routes.py`.

### Mapper + BDX onboarding/ingestion (FastAPI app routes)
| Method | Path | Purpose |
|---|---|---|
| GET | `/data-model` | Return canonical data model used for mapping. |
| POST | `/bdx/sheets` | Inspect uploaded workbook and list available sheet metadata. |
| POST | `/mapper/generate` | Generate proposed mapper from sample Excel and auto-save a draft mapper. |
| PUT | `/mapper/{mapper_id}` | Submit corrected mapping spec and optionally approve it. |
| GET | `/mapper` | List mappers (optional filter by `?mga=`). |
| GET | `/mapper/{mapper_id}` | Get a single mapper (used by UI). |
| GET | `/mapper/{mapper_id}/file` | Download original sample workbook for the mapper. |
| POST | `/bdx/preview` | Apply mapper spec to a new file and return up to 50 mapped rows (no DWH write). |
| POST | `/bdx/upload` | Apply mapper spec and ingest into canonical warehouse. |
| GET | `/uploads` | List ingestion uploads (optional filter by `?mga=`). |
| GET | `/uploads/{upload_id}` | Get upload metadata. |
| GET | `/dwh` | Fetch reassembled canonical policies (optional `upload_id`, `limit`, `offset`). |
| POST | `/export/template/generate` | Upload output sample and draft an export template. |
| PUT | `/export/template/{template_id}` | Update/approve an export template. |
| GET | `/export/template` | List templates (optional `?mga=`). |
| GET | `/export/template/{template_id}` | Get one template. |
| POST | `/export/template/{template_id}/refresh` | Refresh AI mapping for a stored template. |
| POST | `/export/generate` | Generate an output xlsx from approved template + canonical policies. |

### Tenant, parties, programs, users, extra fields (router routes)
| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/login` | Mock login; creates default user/tenant on first login. |
| GET | `/onboarding/status` | Four-step onboarding status summary for a tenant. |
| GET | `/tenants/{mga}` | Get tenant config. |
| PUT | `/tenants/{mga}` | Update tenant config. |
| GET | `/parties` | List parties (search `q`, filter `party_type`, `scope`). |
| POST | `/parties` | Create a party. |
| GET | `/parties/{party_id}` | Get party details. |
| PUT | `/parties/{party_id}` | Update party details. |
| GET | `/parties/{party_id}/contacts` | List contacts for a party. |
| POST | `/parties/{party_id}/contacts` | Create a party contact. |
| DELETE | `/parties/{party_id}/contacts/{contact_id}` | Delete a contact. |
| GET | `/programs` | List programs. |
| POST | `/programs` | Create program. |
| GET | `/programs/{program_id}` | Get program. |
| PUT | `/programs/{program_id}` | Update program. |
| POST | `/programs/{program_id}/contracts` | Upload contract (AI extraction stub). |
| GET | `/programs/{program_id}/contracts` | List uploaded contracts for a program. |
| GET | `/dashboard/stats` | Dashboard stats for the home screen. |
| GET | `/activity` | Activity feed/events. |
| GET | `/extra-fields` | List extra-field definitions (own + shared). |
| POST | `/extra-fields` | Create/update an extra field definition. |
| POST | `/extra-fields/{key}/adopt` | Adopt a shared extra field into the tenant. |
| GET | `/users` | List users for the tenant. |
| POST | `/users` | Create a user. |
| PUT | `/users/{user_id}` | Update user details. |
| DELETE | `/users/{user_id}` | Delete a user. |

### File upload form fields (common)
Many xlsx upload endpoints accept:
- `mga` (required)
- `file` (required)
- optional `skip_rows`
- optional `sheet_name` (older single-sheet use; current UI primarily uses sheet picker + sheet filtering)

Additionally:
- `/mapper/generate` supports optional `sheets` (comma-separated sheet names)

---

## Components

| Concern              | Library / model                                           |
=======
## Components

| Concern              | Library / model                                           |

| Concern              | Library / model                                           |
| -------------------- | --------------------------------------------------------- |
| Web framework        | FastAPI + Uvicorn                                         |
| Excel parsing        | pandas + openpyxl                                         |
| Embeddings           | `sentence-transformers/all-MiniLM-L6-v2` (local)          |
| LLM                  | Gemini `gemini-2.5-flash` via `google-genai`              |
| Mapping execution    | `glom`                                                    |
| Persistence / DWH    | SQLAlchemy + SQLite                                       |

---

## Changelog

Keep this section current on every change.

- **v0.11** — Added **Domain 1 (Parties & Roles)** and **Domain 2 (Contracts
  & Programs)** to the canonical model. Total now **321 fields across 26
  tables**: `tenant`, `party`, `party_address`, `party_contact`,
  `party_license`, `party_relationship`, `program`, `contract`,
  `contract_party`, `contract_terms`, `contract_amendment`,
  `field_requirement_rule` — plus the existing 14 Domain 3 + 4 tables.
  Breakdown: `bdx`=211 (mappable from headers), `fk_resolve`=22
  (name → FK lookup), `system`=88 (PKs / tenant_id / FK ids / ETL audit).
- **v0.10** — Expanded data model to **180 fields** covering every column
  across 14 warehouse tables (Domain 3 + 4): `policy`, `coverage`, `layer`,
  `layer_participation`, `policy_attributes`, `insured_location`,
  `parametric_coverage_detail`, `upload`, `premium_transaction`, `commission`,
  `policy_fee`, `tax_or_surcharge`, `accruals_booked`, `fx_rate`. Each field
  carries a `source` flag — `bdx` (116, mappable from file headers),
  `fk_resolve` (11, name → FK at load time), or `system` (53, PKs/tenant_id/
  audit timestamps; loader-populated). Embedder + LLM only see the BDX +
  fk_resolve subset (127 fields), so PKs and audit columns never appear as
  mapping candidates.
- **v0.9** — Canonical model now targets the **warehouse SQL schema** (Domain
  3 + 4). Every field carries `{table, column, type, description}` so the
  saved Mapper spec maps directly to DB destinations. `apply_spec` produces
  **DB-shaped JSON** (one nested object per table: `policy`, `coverage`,
  `layer`, `layer_participation`, `insured_location`,
  `parametric_coverage_detail`, `premium_transaction`, `commission`,
  `policy_fee`, `tax_or_surcharge`, `accruals_booked`). Embedder context now
  includes `table.column` for better disambiguation between similar fields
  (e.g. policy vs coverage effective dates). Added helpers
  `field_destination(canonical)` and `group_spec_by_table(spec)` in
  `data_model.py`.
- **v0.8** — Removed `POST /mapper/save`. The lifecycle is fully covered by
  `POST /mapper/generate` (creates) and `PUT /mapper/{id}` (updates / approves).
- **v0.7** — `/mapper/generate` response trimmed and re-keyed by **source
  column** (one entry per source) — total entries now equals the number of
  source columns in the file. Dropped the verbose `candidates` blob and the
  redundant `unmapped_sources` list. Added `canonical_unmapped` (slim list of
  canonical names the spec didn't fill). `unsuccessful` and weak `likely`
  entries carry `suggestions` (top-3 candidate canonical names).
- **v0.6** — Rewrote the canonical data model. Now grounded in the
  authoritative `POLICY - PREMIUM BDX Fields.xlsx` plus headers from real
  sample BDX files. Full meaningful names (e.g. `policy_number`,
  `policy_effective_date`, `gross_written_premium`) replacing short DB-style
  abbreviations. Grouped into Policy ID, Insured, Program & Distribution,
  Underwriting, Limits & Layer, Exposure, Location, Premium & Finance,
  Brokers, Reinsurance.
- **v0.5** — `/mapper/generate` now requires `mga` and **auto-saves** the
  proposed mapper (unapproved). Mapping is **header-only** — sample row values
  are returned for display but never used to drive matching. Response groups
  canonical fields into `successful` / `likely` / `unsuccessful` buckets by
  embedding score (≥0.65 / ≥0.45 / else). Added `PUT /mapper/{id}` so the MGA
  can submit corrected mappings and approve them.
- **v0.4** — Added optional `sheet_name` and `skip_rows` form fields on
  `/mapper/generate`, `/bdx/preview`, and `/bdx/upload` so MGAs can target a
  specific sheet and skip leading title/blank rows. `sheet_name` accepts either
  a sheet name or a zero-based index as a string.
- **v0.3** — Switched LLM provider from Anthropic to **Gemini** (`google-genai`,
  `gemini-2.5-flash`). Env var `GEMINI_API_KEY` (with a compiled-in fallback
  key for dev). Updated requirements.
- **v0.2** — Replaced placeholder canonical data model with a clean,
  homogeneous mirror of the `coverage` table from `Data Model.pdf` (the premium
  table). Each field carries `{type, description}`. ETL/audit columns excluded.
- **v0.1** — Initial scaffold: `/mapper/generate`, `/mapper/save`, `/mapper`,
  `/bdx/preview`, `/bdx/upload`, `/dwh`. Embedding + LLM mapping with greedy
  fallback. Glom-based ingestion. SQLite persistence.
