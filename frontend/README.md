# Kavachio Frontend (React + Vite)

This is the UI for the Kavachio onboarding + mapping + ingestion workflow.

## Tech
- React 18
- React Router
- TypeScript
- Vite
- TailwindCSS

## Prerequisites
- Node.js 18+

## Setup
```bash
cd frontend
npm ci
```

## Configure API base URL
Create or update a Vite env file (example):

```bash
# frontend/.env
VITE_API_URL=http://localhost:8000
```

If you don’t set it, the app defaults to: `http://localhost:8000`.

## Run (dev)
```bash
npm run dev
```

App URL (typical): http://localhost:5173

## Build
```bash
npm run build
```

## App screens (routes) and purpose

> Authentication is handled by `src/auth.ts` and guarded in `src/App.tsx`.

### Public
- **/login** (`src/pages/Login.tsx`)
  - User sign-in / session setup.

### Authenticated
- **/welcome** (`src/pages/Welcome.tsx`)
  - First-run landing / onboarding context.

- **/home** (`src/pages/Home.tsx`)
  - Dashboard entry point (overview cards / navigation).

- **/tenant** (`src/pages/Tenant.tsx`)
  - Tenant-level configuration and context.

- **/parties** (`src/pages/Parties.tsx`)
  - List of parties.

- **/parties/:id** (`src/pages/PartyDetail.tsx`)
  - Party details.

- **/programs** (`src/pages/Programs.tsx`)
  - List of programs.

- **/uploads** (`src/pages/Uploads.tsx`)
  - View and manage ingestion uploads.
  - Also shows saved mapper drafts for the current MGA (used to re-open mapping).

- **/uploads/mapper/:mapperId** (`src/pages/Mapping.tsx`)
  - Core mapping review screen.
  - Loads mapper via `GET /mapper/{mapperId}`.
  - Loads canonical schema via `GET /data-model`.
  - Allows selecting canonical fields per source header (top AI candidates + full data-model picker).
  - Saves:
    - **Save draft** → `PUT /mapper/{id}` with `approved=false`
    - **Save & continue** → `PUT /mapper/{id}` with `approved=true`

- **/outputs** (`src/pages/Outputs.tsx`)
  - Export management UI.

- **/outputs/templates/:id** (`src/pages/OutputTemplate.tsx`)
  - Output/template editor and approval.

- **/users** (`src/pages/Users.tsx`)
  - User management.

## End-to-end workflow in the UI

1. Login → reach app shell (guarded routes).
2. Upload a sample BDX (Excel) through the sample/mapping flow (driven by backend endpoints such as `/mapper/generate`).
3. Review header-to-canonical mapping on **/uploads/mapper/:mapperId**:
   - Per source column, choose the canonical field.
   - Use confidence pills/bars to decide quickly.
   - If none match, search the full canonical data model and pick manually.
4. Approve mapping (Save & continue).
5. Run preview and ingestion using the backend (endpoints under `/bdx/*`).
6. Generate exports using approved templates (backend endpoints under `/export/*`).

