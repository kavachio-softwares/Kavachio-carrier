# Kavachio Backend (high-level)

Backend is implemented as multiple services under `backend/`.

- `backend/python-services/`
  - **BDX Mapper + Ingestion** (FastAPI)
  - Responsible for:
    - generating column→canonical mappings from an MGA’s sample BDX Excel
    - persisting mappings as glom specs
    - previewing and ingesting BDX rows into the canonical relational warehouse
    - generating export workbooks from approved export templates

- `backend/js-services/`
  - Node.js services (e.g., extraction/validation) — see `backend/js-services/README.md` if present.

## Primary backend doc

Use:
- `backend/python-services/README.md`

It contains full setup instructions and the complete API reference for the FastAPI service.

