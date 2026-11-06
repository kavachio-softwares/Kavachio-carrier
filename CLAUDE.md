# CLAUDE.md — Kavachio Carrier

Standing instructions for Claude Code in this repo. Keep this file short: rules and facts that stay true for months.
History, feature detail and the change log live in `PROJECT_CONTEXT.md` (read it before working on an area).
If this file and the code disagree, the code wins — then fix this file.

Written 2026-11-03 against `origin/development` @ `74fe1ff`; updated 2026-11-04 on `development-phase-2`.
Everything here applies to both branches. Phase 2 (`development-phase-2`) adds areas this file does not describe yet:
reinsurance (`ceded_*.py`, `treaty_*.py`, `/reinsurance/*` routes) and Policy Search (`policy_search.py`, `/policies`, 43.1).
`PROJECT_CONTEXT.md` does not describe them either — ask their authors before changing them.

## What this is

A bordereaux (BDX) platform: carriers hold contracts with brokers, Kavachio turns contract clauses into checks,
takes brokers' monthly files in (portal, email, SFTP, API), checks them, produces the carrier's output BDX,
and sends exceptions back to the broker to fix.

Hierarchy: Kavachio → Carrier (tenant) → Programme → Broker → Contract → Bordereau Setup → Files / versions / exceptions.

## Layout

| Path | What |
|---|---|
| `backend/python-services/` | **The live backend.** FastAPI, entry `main.py`. SQLAlchemy ORM in `db.py`, plain-SQL `migrations/`, DuckDB checks, Gemini AI steps, `test_*.py` (pytest). |
| `frontend/` | React 18 + TypeScript + Vite + Tailwind. Routes in `src/App.tsx`, roles in `src/access.ts` / `src/auth.ts`. |
| `backend/Microservice/` | Old September split that has drifted. **Do not edit**; fixes there never reach the live app. |
| `backend/assests/` | Bundled reporting standards (spelled `assests`). |

## Commands

```bash
# Backend (from backend/python-services)
python main.py                      # API on :8000, auto-reload, starts the SFTP server on :2022
./.venv/bin/python -m pytest test_x.py -q

# Frontend (from frontend) — needs Node 22; the system node is too old
export PATH="$HOME/.nvm/versions/node/v22.23.0/bin:$PATH"
npm ci
npm run dev                         # :5173
npx tsc --noEmit                    # type-check
npm run build
```

Docker: `backend/python-services/Dockerfile` (python 3.12, Tesseract OCR, `uvicorn main:app`, broker emails ON in the image).

## Environment

- Config comes only from environment variables (`settings.py`, `os.getenv`). `main.py` loads `.env` without overriding, so an exported variable wins.
- `.env` is gitignored: a setting on one PC does not travel. Check it before calling a feature broken.
- `DATABASE_URL` is the only database setting. `VITE_API_URL` is baked into the frontend at build time.
- `JWT_SECRET` and `INTAKE_KEY_PEPPER` fall back to insecure dev values when unset. Both must be set on any real deployment, and changing either later breaks existing logins, links, API keys and SFTP passwords.
- **Do not add, remove or rename `.env` keys** unless the user asks. **Do not add `XYZ_ENABLED` feature switches** — new features ship on. Existing switches are listed in `PROJECT_CONTEXT.md` §4.
- No hardcoded values: derive from the setup mapping, data model, contract terms (`contract_types.py`, served to the UI, never restated) or env. Report a config gap; don't paper over it.

## Database — precautions

- **The dev database at 192.168.2.11 is shared by the whole team**, and the QA database is shared with another app's tables.
- `python main.py` runs `init_db()` (`create_all` + column top-ups) on every start and every auto-reload. Saving `db.py` changes the shared database's tables.
- **Never run tests, scripts or flows that write against the shared DB or QA.** Test data has leaked there several times. For write flows, use a disposable local Postgres copy (pg_dump → restore locally, API on another port).
- **Check a test before running it.** A `test_*.py` that imports `main`, uses `TestClient(main.app)` or opens `SessionLocal` writes to whatever `DATABASE_URL` points at, and most never clean up (22 files import `main`; `test_ceded.py` leaked two test carriers on 4 Nov). Run those only against a local copy.
- Read-only checks: connect with `options="-c default_transaction_read_only=on"` (or `PGOPTIONS`).
- **A person applies migrations, deliberately, after an explicit yes.** Before any change on a shared DB: full backup, test on a local copy, before/after row counts. Never delete or truncate records without approval.
- `TRUNCATE party … CASCADE` also empties `app_user`.
- Migrations are plain SQL in `backend/python-services/migrations/`. Tables created first by the ORM skip the inline constraints and defaults of `CREATE TABLE IF NOT EXISTS` — check the result, not just "no error".
- Stored times are UTC (`datetime.utcnow`); the dev DB's timezone is Asia/Kolkata. Compare in UTC.
- Column names are entity-prefixed (`user_email`, `contract_program_id`); the ORM and `align_to_database` handle it.
- Reuse existing tables before adding new ones; justify every new table.

## Business rules that must not break

- **Three seats:** `kavachio_admin`, `carrier_admin`, `broker_admin`. Carrier users and operators were retired (29 Sep 2026) — don't rebuild them without a product decision.
- On screen, emails and PDFs say **"Carrier"** and **"Broker"**, never "Carrier Admin" / "Broker Admin". Codes keep the long names.
- **Only a broker amends exceptions** (`carrier_scope.assert_can_amend`, `canAmendExceptions`). Kavachio and carriers are view-only.
- **Broker A must never see broker B's data.** Broker-facing routes check the broker's programmes (`carrier_scope`, `assert_broker_on_programme`). Audit visibility goes through `audit_feed.scope_for` only.
- A contract needs a broker; a Bordereau Setup needs a broker and a contract.
- A contract upload only extracts clauses; it never maps. Mapping happens only in Bordereau Setup.
- A contract is checked on nothing until it is bound. `write_rules` deletes a contract's rules before writing.
- Every file names programme + contract + month; a resend for the same month is the next version. Fix & Validate creates a new version; old versions are kept.
- Safety checks (size, safe to open, malware) run before anything else opens a file.
- Counts come from one place (`exception_tally.py`, `broker_tally.py`) so every screen agrees.
- Output BDX mandatory fields are enforced when a template is activated, never when a file is produced.
- **Read a processed file's columns by their heading, never by the template's `column_index`.** The writer drops empty columns (a 145-column template can give a 62-column file), and older files predate later template edits, so positions shift. The highlighter does it right (`exporter.highlight_exceptions`, using `output_template_fields.header_of`); `ceded_engine.read_export_rows` does not yet (15 of 601 current files misread, fix pending).
- AI answers are cached by content (`ai_cache`) so the same input gives the same result. Don't bypass the cache.

## Coding conventions

- Match the surrounding code: naming, comment density, idiom. Prefer one code path over two copies.
- Paging is opt-in (`page` / `page_size`); without `page` a response stays the same. Per-row filters run before the page is cut.
- UI wording: plain, short, no jargon. Explanations go behind the ⓘ icon (`InfoTip`, `StatCard info`), not grey lines under labels. Use the app's own popups.
- New audit action names go in `audit._FRIENDLY`.

## How to work here

- **Analysis or a plan was asked for → propose and wait for an explicit "yes" before changing code.** Show proposed on-screen wording before applying it.
- Lead reports with a one-line plain-words verdict, then the technical detail.
- Before starting: `git log --oneline -15` and read the `PROJECT_CONTEXT.md` section for your area. Don't redo or undo others' in-flight work; don't touch another developer's staged files.
- Commit or push only when asked. Commit messages: short bullets a non-technical reader understands. Never commit `pipeline_decisions.log` (tracked despite `.gitignore`), `.env`, `certs/` or `*.p12`.
- Never put passwords, keys or connection strings in chat, commits or docs.
- After an approved build, run the requester's own demo file through the same screen and route.
- Branches, by phase:

  | Branch | Phase | Notes |
  |---|---|---|
  | `development` | **Phase 1** | Where Phase 1 work lands; `qa` is built from it. |
  | `development-phase-2` | **Phase 2** | Phase 2 work (e.g. reinsurance, MOPR). Its code and migrations are **not** on `development`. |

  Before changing anything, check which branch is checked out and keep each change on the branch of its phase. Don't bring Phase 2 code, migrations or database changes into `development` or QA unless the team asks.

## Keeping this file current

After finishing a change, check whether it alters anything stated here: a command, a folder, an env key,
a database procedure, a seat or permission, or a business rule above. **Update this file only then**, in the same
change. Ordinary features and fixes go in `PROJECT_CONTEXT.md`'s change log, not here.

## Needs confirmation (not verified — ask before relying on it)

1. `PROJECT_CONTEXT.md` exists on `development-phase-2` but **not on `development`** yet. Confirm it will be merged, or this file's references to it point nowhere on `development`.
2. **Next migration number.** `development` has up to `39`, then `42_mid_term_change.sql` (36, 40, 41 unused). `development-phase-2` goes up to `46` with duplicate numbers (three `42_*`, two `43_*`, two `44_*`; `46_monthly_report_delivery.sql` added 5 Nov). The team needs to agree on a numbering scheme before the next migration.
3. Who applies migrations to QA/UAT, and whether a migration runner or record of applied migrations exists (none found in the repo).
4. Deployment target and process (the Dockerfile mentions a VM with `/opt/kavachio/.env` and an App Gateway; no CI workflow exists in `.github/`).
5. Production SFTP approach (built-in server vs Azure Blob SFTP) and production storage (`STORAGE_BACKEND=db` vs `azure`).
6. The known-failing tests (`test_autorun_no_setup.py`, noted 16 Oct) — whether they still fail on `development`.
