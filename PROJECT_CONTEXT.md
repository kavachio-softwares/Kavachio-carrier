# Kavachio Carrier — Project Context

> **Read this first, in every session, before changing anything.**
> It is the shared memory of everyone working on this repo (people and Claude sessions).
> **Update it in the same commit as your change.** See "How to keep this file current" at the bottom.

Last full review: **2026-10-16**, against commit `53065df` (cross-checked twice).
Facts come from the code, the git history, the team's working notes and the plan documents next to the repo. Anything marked **(verify)** came from notes and may have moved since; check the code before relying on it. **If this file and the code disagree, the code wins. Then fix this file.**

---

## 1. What the product is (plain words)

Kavachio is a platform for **bordereaux (BDX)**. A *bordereau* is a spreadsheet a broker sends a carrier (insurer) each reporting period, usually monthly. It lists policies, premiums and transactions.

Kavachio does six jobs:
1. Holds the **contract** between a carrier and a broker: terms, wording, signatures, renewals.
2. Reads the contract and turns its clauses into **checks (rules)**.
3. Lets the carrier set up how a broker's file **maps** onto the carrier's layout (**Bordereau Setup**).
4. Takes the broker's files in (portal, email, SFTP, API), **checks** them, and produces the carrier's **output BDX**.
5. Tells the broker what is **wrong (exceptions)**, so the broker fixes it, and keeps every version.
6. Tracks **what is due, late or done** (calendar, reminders, dashboards, audit trail).

## 2. Who uses it (three seats)

| Seat (code) | On screen | Can do |
|---|---|---|
| `kavachio_admin` | Kavachio | Onboard carriers, see everything, Data Mapping Queue, platform Rule Library. **View only** on exceptions. Cannot open a carrier's contracts or invite its brokers. |
| `carrier_admin` | **Carrier** | Programmes, brokers, contracts, Bordereau Setup, channels, Rule Library, calendar, files. **View only** on exceptions. |
| `broker_admin` | **Broker** | Agrees and signs contracts, processes bordereaux, **fixes exceptions**. One broker login per broker firm. |

- On screen, emails and PDFs say **"Carrier"** and **"Broker"**, never "Carrier Admin" or "Broker Admin". The codes keep the long names.
- **Carrier users and broker users (operators) were retired on 29 Sep 2026.** Their login is refused and creating them returns 410. `operator` stays in the DB role check only for leftover rows. Do not rebuild these seats without a product decision.
- **Only a broker amends exceptions** (`assert_can_amend` / `canAmendExceptions`). Kavachio's own processing screen `/direct` is closed to carriers.
- People at the broker who handle files have no login. They use **secure links** (section 8.8).
- A broker can work with several carriers (section 5).

## 3. Running it

- **Backend:** Python / FastAPI in `backend/python-services/` (entry `main.py`). Postgres through SQLAlchemy plus plain-SQL migrations. DuckDB runs the checks. Gemini runs the AI steps.
- **Frontend:** React 18 + TypeScript + Vite + Tailwind in `frontend/src/`.
- **`backend/python-services` is the live backend.** `backend/Microservice/` is an early-September split that has drifted (no intake, audit, broker routes or `/v1`). Fixes in one do not reach the other. Edit `python-services` only.
- Ports: API **8000** (`python main.py`, which auto-reloads), frontend **5173**, Kavachio's own SFTP server **2022**. CORS allows 5173; add others with `CORS_ORIGINS`.
- `main.py` loads `.env` without overriding, so an exported `DATABASE_URL` wins. `DATABASE_URL` is the only database setting.
- **Node:** the system node (v12) cannot build the frontend. Use Node 22: `export PATH="$HOME/.nvm/versions/node/v22.23.0/bin:$PATH"`, then `npm ci`. Type-check with `npx tsc --noEmit` in `frontend/`.
- **Scanned contract PDFs need Tesseract OCR** (installed in the Dockerfile). Without it a scanned contract fails upload with `INSUFFICIENT_TEXT`.
- **Storage:** `STORAGE_BACKEND=db` (default) keeps file bytes in Postgres. `azure` uses Azure Blob, or Azurite locally (see `README-storage.md`).
- **Signing certificates** (`certs/`, `*.p12`) are gitignored. Make a local one with `scripts/make_dev_signing_cert.py`.
- **Python gotcha:** a stale `protobuf-3.6.0-nspkg.pth` can break `import google.genai`. The fix needs root: remove that `.pth` file.
- `.env` is **gitignored**, so a switch set on one machine does not travel. Check the switch before deciding a feature is broken.
- First Kavachio admin: `scripts/create_kavachio_admin.py` (`KAVACHIO_ADMIN_EMAIL` / `_NAME` / `_PASSWORD`).

## 4. Feature switches (environment)

Read from the environment. "On" means `1/true/yes/on`.

| Switch | Default in code | What it does |
|---|---|---|
| `CARRIER_APPROVALS_ENABLED` | off | Carrier-admin approval screens (broker onboarding, setup approval, accepting a carrier user's upload). Dormant now that carrier users are retired. |
| `MULTI_CARRIER_BROKERS` | off (on in the main dev `.env`) | Lets a second carrier invite a broker who already works with another carrier. |
| `BROKER_NOTIFY_ENABLED` | off; **on in the Docker image** | Emails brokers what happened to files sent by email, SFTP or API. Portal uploads are never emailed. |
| `EMAIL_REPLY_ON_REFUSAL` | off; **on in the Docker image** | Replies to an unknown sender whose emailed file was refused. |
| `MAIL_ALLOWED_RECIPIENTS` | blank | Test guard. When set, mail goes only to the listed addresses. Must be blank in production. |
| `BORDEREAU_AUTO_SEND_EMAIL` | **off** | Emails the processed bordereau to the carrier. Only for a broker portal run that ticks a calendar period. Email/SFTP/API arrivals are never forwarded. |
| `CONTRACT_ASOF_ENABLED` | off | Prior Period Files: check rows against the contract version in force on their date. A setup can switch it on for itself. |
| `RENEWAL_CHAIN_ROUTING` | on | Routes a file across a renewal chain (section 6.5). |
| `RENEWAL_OVERLAP_CHECK` | on | Refuses renewal dates that overlap the contract being renewed. |
| `RENEWAL_WINDOW_DAYS` | 30 | How many days before expiry an active contract may be renewed. |
| `CONTRACT_PERIOD_CHANGE_ENABLED` | off | Change period: correcting a contract's dates (section 6.6). |
| `PERIOD_CORRECTION_LARGE_DAYS` | 31 | A correction larger than this needs evidence and the typed contract code. |
| `INTAKE_AUTORUN_ENABLED` | on | Runs arrived files automatically. |
| `INTAKE_LIVE_ENABLED` | on | Pushes new arrivals to an open Files screen. |
| `INTAKE_RETENTION_ENABLED` | on | Purges copies of held or rejected files after 90 days. |
| `INTAKE_AV_BACKEND` | `none` | Virus scanning. `clamav` is supported. **By default there is no virus scan.** |
| `INTAKE_ALLOW_MACROS` | on | Accepts macro-enabled workbooks. |
| `INTAKE_SECRET_KEY` | derived from `JWT_SECRET` (verify) | Encrypts saved SFTP passwords and keys. |
| `EMAIL_INTAKE_ACCOUNT` | none | Which mailbox the intake reads. Today it is a **test** mailbox; a dedicated one is planned (`INTAKE`). |
| `EMAIL_POLLER_ENABLED`, `EMAIL_IDLE_ENABLED` | on | Mailbox reading (IMAP IDLE plus an hourly sweep). |
| `SFTP_SERVER_ENABLED` | on | Kavachio's own SFTP server (`SFTP_SERVER_PORT`, default 2022). |
| `SFTP_PULL_ENABLED` | on | Collecting from a broker's own server. `SFTP_PULL_ALLOW_PRIVATE` defaults off and blocks private addresses. |
| `SFTP_POLLER_ENABLED`, `SFTP_WATCH_ENABLED` | on | Picking files out of SFTP folders. |
| `SWEEP_SCHEDULER_ENABLED`, `SWEEP_EMAIL_ENABLED` | on | Daily deadline sweep and reminder emails (`SWEEP_HOUR_UTC`). |
| `STORAGE_BACKEND` | `db` | `azure` = blob storage; anything else = bytes in Postgres. |
| `KAVACHIO_RLS` | off | Postgres row-level security. |
| `KAVACHIO_AI_CACHE` | on | Caches AI answers so the same input gives the same result. |
| `JWT_SECRET` | dev secret when unset | **Must** be set on any real deployment. |

Mapping thresholds can be tuned with `KAVACHIO_MAPPING_AUTO_CONFIDENCE`, `_REVIEW_CONFIDENCE` and `_CANDIDATE_SIMILARITY`. Many more `KAVACHIO_*` settings tune the AI calls; grep before adding a new one.

## 5. Business hierarchy and approvals

```
Kavachio (platform)
 └─ Carrier (tenant)
     └─ Programme   (a book of business; may have a business segment)
         └─ Broker   (linked to the programme)
             └─ Contract   (carrier ↔ broker, for a term; renewals form a chain)
                 └─ Bordereau Setup   (mapping + rules + output template)
                     └─ Files → a version per reporting month → exceptions → output BDX
```

- **A contract needs a broker** (upload and create refuse without one), and **a Bordereau Setup needs a broker and a contract**. The setup list shows only programmes that have a broker with a contract. Two legacy "Auto contract for program …" rows (7518/7519) are placeholders the loader made, not user contracts.
- A broker's contract can be reused for another broker on the same programme, as that broker's own copy.
- **Multi-carrier brokers:** one code path. The switch only gates inviting an existing broker. The second carrier sends an invitation, and the broker accepts at `/invitations` signed in as themselves. Brokers switch carriers in the sidebar (`BrokerCarrierSwitch`). Since `53065df`, a broker shared between carriers counts as a carrier's broker when the two have an active relationship, so contracts with each carrier can be created, renewed and made active (this unblocked contract 7758).
- **Removing a broker** is per programme (Broker page → Programmes → Remove). The link is marked inactive (or deleted when there are no contracts), the broker's admin contacts are emailed, and their write routes and fix links are blocked (`assert_broker_on_programme`). Adding them back reactivates the link and emails **only a returning broker**, never on a first add.
- **Approvals:** with `CARRIER_APPROVALS_ENABLED` off (the default) and carrier users retired, **the carrier admin's own act is the decision**. Their upload is active at once, their setup goes live, and the approval screens stay hidden. If the switch ever goes back on, three separate gates exist and **must never be merged**: broker onboarding (who we work with, migration 28), Bordereau Setup approval (what they may send, migration 27), and accepting a carrier user's uploaded contract.

## 6. Contracts

Code: `contract_routes.py` (the record and its lifecycle), `contract_types.py` (fields, limits and term rules; **served by the backend, never restated in the UI**), `contract_wording.py`, `renewal_chain.py`, `period_change.py`, `esign_*.py`, `contract_upload_services/`. UI: `Contracts`, `ContractNew` (wizard), `ContractRecord` (tabs Summary / The contract / Clauses, plus a Documents card), `SignContract`, `ContractSignatures`.

### 6.1 Two ways a contract is born
- **Authored** in Kavachio (`/contracts/new`). The carrier raises it, the broker reviews and agrees (or asks for changes), then the **carrier signs first and the broker signs second**. The contract becomes active and Bordereau Setup opens. A line at the top of the record says whose turn it is.
- **Uploaded** (a PDF signed on paper). The carrier admin's upload is **active at once**. Uploaded contracts are accepted, never signed in Kavachio, and show no signature buttons. Documents under about 100 words are refused as too short.
- Contract types are coded (`insurer_broker`, `insurer_reinsurer`). Uploads used to store the PDF heading as the type, which broke Renew (fixed by migration 37).
- A contract's dates can be edited only while it is draft, pending or changes-requested. After that, use Change period (6.6).

### 6.2 Clauses, checks and AI
- **An upload only extracts clauses; it never maps.** Mapping happens only in Bordereau Setup.
- AI pipeline: Call 1 extracts clauses, Call 2 finds which clauses carry a rule (cached as `rule_intents`), and Call 3 binds rules to template fields. Answers are cached by content (`ai_cache`): `clause_extraction` is keyed on prompt and model, so the same PDF gives the same clauses. Without the cache, one PDF gave 41 or 59 clauses on identical runs, which is normal model variance.
- A term agreed after the wording was written gets its own clause ("wording top-up"). Terms only; deletions stick.
- **A contract is checked on nothing until it is bound** to the output template it reports into (`POST /contracts/{id}/bind-checks`). Caution: `write_rules` deletes every rule on the contract first, so a PATCH with new limits on a contract that has rules swaps them out. PATCH is allowed only in draft, pending or changes-requested.
- Clauses that point at a document nobody supplied cannot become rules. An orange banner names the missing documents.
- **Documents card** on the contract record. **Reference**: a document the contract refers to, like underwriting guidelines; attaching it clears the banner. **Wording**: a new version replaces the old, and "This is the signed copy" marks the executed copy. **Endorsement**: a change agreed later that sits beside the wording. The empty card shows two short bullets (14 Oct).

### 6.3 E-signing (real code)
- Carrier signs first, then broker. More than two signers are allowed. Only the broker admin signs for the broker. Signature blocks are keyed by person (`carrier#2`).
- Signers open an emailed link and unlock it with a **one-time code** (`esign_otp.py`; attempts, lockout and link lifetime set by `ESIGN_*`). The finished PDF is **sealed** with a real digital signature (`esign_seal.py`). Four emails per round (`esign_email.py`).
- Signing links are never stored in the audit trail.
- Gotcha fixed: PyMuPDF `insert_textbox` silently writes nothing when text does not fit the box by height, so typed signatures once came out blank for the other party.

### 6.4 Renewal (committed in `0be18f1`, extended in `53065df`)
- The **Renew** button appears on an active contract only within `RENEWAL_WINDOW_DAYS` (30) of expiry. It reads **"Create renewal"**.
- Renew copies the commercial terms and wording clauses (not documents) into a **new contract row** pointing back (`renews_contract_id`). Copied rules are re-dated for the new term. Parties are re-checked on renew, send-for-review and accept.
- The old contract is **superseded when the renewal goes active**, not when Renew is clicked. A superseded contract still governs late files from its own term.
- **Typed renewal:** the broker agrees and both sign. **Uploaded renewal:** the Renew form requires the **signed renewal PDF** (attached as Wording, signed copy). A **"Make it active" card** stays on the renewal until it is in force. It lists every reason it can't go in force yet (signed copy, reading the new PDF, references, broker and programme, dates), and pressing it re-checks everything on the server. The broker never "agrees" an uploaded contract in the app.
- **Overlap check** (`RENEWAL_OVERLAP_CHECK`, on): overlapping days are refused, and a gap between the two terms has to be confirmed once. The check runs on create, on date edits and on going in force. The record shows a full-width card where versions overlap or leave a gap, and the Renew form suggests the right start date. Existing overlaps (e.g. 7732/7757) are only reported.
- **Day rule:** every calendar day belongs to exactly one version. A term runs from inception **up to but not including** expiry, so the handover day belongs to the new version.

### 6.5 Files across a renewal chain (`renewal_chain.py`, `RENEWAL_CHAIN_ROUTING`)
- A file naming the **old** contract's code for a month the renewal covers goes to the renewal.
- When it is unclear, the file is **held**. On Files Received, a person picks the contract to run it under and releases it.
- In a run, a setup's contract pin follows the chain to the right contract.

### 6.6 Change period (`53065df`; switch `CONTRACT_PERIOD_CHANGE_ENABLED`, **off**)
- A carrier corrects the start or end date of an in-force or ended contract, after a preview of everything the correction changes. Only the **Correction** kind is built. An agreed-term change (an "Endorsement" kind) is planned but not built.
- Rules holding exactly the old date are re-dated. Reporting periods that already have files are never removed.
- A correction over 31 days needs an **evidence file** and the contract code typed in, and Kavachio is told.
- The broker is emailed the old and new dates with the reason. The internal note is never sent.
- The latest correction can be **undone** while nothing has happened since. Undo puts the dates, rules and reporting calendar back exactly.
- Evidence files are hidden from brokers everywhere. Kavachio opens them only from the correction's row in its Audit Logs.
- Never touched: the termination date and reason, the lifecycle, and the prior-period dates.
- Logic only in `period_change.py`; UI in `ChangePeriodDialog.tsx`; full plan in `change-period-plan.md` next to the repo.

### 6.7 Prior Period Files (late or corrected files)
- With `CONTRACT_ASOF_ENABLED` on (globally or per setup), rows are checked against the contract version **in force on their date**. A row whose date no version covers falls back to today's contract, and its exception names the contract and period used.
- `702ba8d`: a new contract version now closes the previous one's `contract_effective_to` at the new start.

## 7. Bordereau Setup (the carrier configures a broker's file)

Code: `direct_routes.py`, `direct_mapper.py`, `semantic_mapping.py`, `rule_scope.py`, `setup_scope.py`, `output_template_*`, `reporting_standards.py`, `missing_columns.py`. UI: `BordereauSetups`, `DirectSetup` (`/direct/setup`), `BordereauSetupDetail` / `Edit`, `OutputTemplate`, `RuleLibrary`.

- Three steps: **Setup Details → Documents → Output BDX Template**. The Documents and Output BDX steps stay shut until a broker is picked.
- **Mapping:** input columns → output template columns, using exact and name matches plus one AI call. Confidence ≥0.90 maps automatically, above 0.80 is flagged for review, and a failed AI call shows as AI unavailable. Every decision is logged in `pipeline_decisions.log`.
- The setup page shows the carrier's **original sample** (the first landing with the setup's layout), never the newest broker file.
- **Contract Rules** has 4 tabs: Rules, Rules with no column, Columns Missing from BDX, Missing documents.
- **Rule set per output template** (`rule_scope.py`). Old rules are never retired; a setup includes or excludes rules for itself. The backend is done; the per-rule include screens were pending (verify).
- **Output templates** are saved under their **programme + broker + contract**, and brokers never share or overwrite each other's. Bundled standards (Lloyd's Risk & Premium V5.2) live in `backend/assests/` (spelled that way); the folder is **outside the Docker build context** (verify the image has it). **Mandatory fields are enforced when a template is activated, never when a file is produced.** Generation never blocks delivery.
- A blank **Bordereau Input Template** can be downloaded before processing (`bordereau_template.py`). `sample_bordereau.py` builds rows designed to break a contract's own checks, for testing.
- **Rule Library:** one table, `generic_rule_spec` (migration 30 copied the old `generic_rule_specification`; some seed scripts still name the old table, verify). Edits take effect at the **next Bordereau Setup**. Kavachio's standard checks also run on every carrier's files.
- **Contract margin:** one per contract, shown as a tile in the setup and applied at run time. Formula rules without their own tolerance use it (0 = exact to the cent).
- Wide, sparsely mapped templates no longer look "clean": unmapped checks show as not validated / not checked (`validation_outcome.py`).
- **Data Mapping Queue** (Kavachio admin, `/admin/mapping-tasks`): unknown input formats waiting to be mapped into the canonical data model.

## 8. Files in, checks, output

### 8.1 Channels (one pipeline: `intake_service.py`)
- **Portal upload** (broker Process Bordereau `/broker/bordereau`; Kavachio's `/direct`).
- **Email:** the broker emails **Kavachio's mailbox** and Cc's a carrier person; From + Cc pick the route, and with no Cc the file is turned away with the reason. Gmail-specific reading (peek, labels). The current mailbox is for testing only.
- **SFTP, main option:** Kavachio's own server (`sftp_server.py`, port 2022). Each broker gets an emailed login and can only upload to `/incoming`; replies appear in `/outbound`. Brute-force blocking and weak ciphers are refused. AWS SFTP is the long-term plan.
- **SFTP, second option:** Kavachio logs in to the broker's own server (`sftp_pull.py`). The host key is pinned at Test Connection and passwords are encrypted (migration 35).
- **API:** `/v1/*` with a key per channel. "Email Instructions" sends the broker a fresh key and a ready-to-copy example. `/v1/submissions/{ref}` returns status and exceptions.
- **Cloud folder exists only as a label.** It is not built.
- Channels can be configured before any Bordereau Setup exists (section 8.3).

### 8.2 At the door
- Every file must name its **programme, contract and month**. Short programme/contract codes are used for email and SFTP. A resend for the same month becomes the **next version**.
- **Safety checks run before anything opens the file** (`intake_safety.py`): size, safe to open, then malware (none by default, section 4). The security scan runs before the duplicate check.
- The required-columns check (`intake_required_fields.py`) runs for email, SFTP and API files.
- Files from email, SFTP and API are **kept in the DB** (`file_arrival_file`, migration 31) until processed, so they can always be run again. Portal uploads are not double-stored.
- Held files can be **released or discarded** by a person, and who did it is recorded (`intake_review.py`).
- Screen words (approved 30 Sep): **Channel, Accepted, On Hold, Rejected, Processing, Clean**. Buttons: Ingestion Channels / Ingestion Panel / Add Ingestion Channel. The sidebar Ingestion dock shows a count, red when a run failed.

### 8.3 File before setup
A file that arrives before its Bordereau Setup exists is kept. The carrier gets one "Setup needed — file waiting" bell notice per programme and broker, and the file **runs by itself** when the setup goes live (`intake_autorun.requeue_waiting`). The bell notice does not clear itself.

### 8.4 Layout change gate (broker portal runs only)
At least **90%** of the setup's columns must match (`LAYOUT_MIN_MATCH_PCT`). Below that, a popup offers Cancel or Process Bordereau Anyway. After the run, a card lists **Missing** and **Extra** columns. Email, SFTP and API runs are not gated.

### 8.5 The run
`direct_routes._prepare_run` → ITD step (8.9) → checks (DuckDB) → `_render_landing` → output BDX → processed rows loaded into the data model (`datamodel.ingest`). This is the **direct lane**: corrections are kept in `landing_correction`, with no write-back to canonical tables. All counts come from one place (`exception_tally.py`, `broker_tally.py`), so every screen shows the same numbers.

### 8.6 Sending to the carrier
`BORDEREAU_AUTO_SEND_EMAIL` is **off**. Sending still releases the version and marks the calendar period sent; only the email is skipped, and those skipped rows are hidden from Audit Logs.

### 8.7 Exceptions
- The review shows a short summary first, with full rule details one click away. **Only the broker amends.** Every decision is stored permanently with who made it (`exception_decision_log`).
- A sum rule pins **one** exception to the first row of the group and labels it **"File total"**. Premium, fee and tax limits are judged on a **policy's total**. A wider "policy acts as one" rule was **reverted on request**; do not re-add it unless asked.
- Downloaded Excel highlights failing cells (resolved by header name) and has a **Results** sheet.

### 8.8 Broker exception loop: secure link, Fix & Validate, Results
- The broker is emailed (switch, section 4) with the highlighted BDX attached and a **secure link** (HMAC-signed with `JWT_SECRET`, `/fix/:token`). Validate runs a throwaway check. Submit lands the corrected data as the **next version**, like an API upload.
- **Fix & Validate** in the portal also creates the **next version**; the old one stays as "Replaced". It is refused for a delivered bordereau or a version that is no longer current.
- **Results** tab, Excel Results sheet and CSV: passed, failed, resolved, not checked.
- Programmes carry a delivery rule and deadlines. A daily sweep marks overdue files and sends reminders (`sweep_scheduler.py`). Each month shows **once, as its latest version**, everywhere.

### 8.9 ITD bordereaux (commit `df20924`)
Some brokers send **cumulative (inception-to-date)** files. Kavachio sends the carrier **only what is new or changed**.
- A file is ITD or monthly **by its rows, never its name**. A new setup turns ITD on by itself when its sheets have an invoice number (or policy + transaction type + date) and a reporting period column. Settings live in `pipeline.itd_config`.
- One running list of everything already sent per carrier + programme + broker (`bdx_sent_transaction`). Each file's row results go to `bdx_row_comparison`.
- Rows are New, Previously processed, Changed, Cancelled or reversed, or Skipped. An ITD file sends new rows, reversals (`-R`, linked to the original) and one `-ADJ` row per changed amount. A monthly file is sent whole. Late transactions in a monthly file are still loaded.
- Transactions missing from a new ITD are listed for the carrier: **Mark as removed** or **Keep as active**. An older ITD after a newer one is refused. A corrected version replaces an undelivered earlier one. **Undo this file** removes a wrong upload.
- Two steps: `stage()` when the file lands and `apply()` after output is produced, so a failed run never double counts.
- UI: ITD / Regular badge on Files Received, a **View comparison** drawer, and an ITD card on the run result.
- ⚠ **On the shared DB the ITD history still sits under the old table names** (`bordereau_position*`) while the app reads the empty `bdx_*` tables. The next ITD file would be resent whole. A repair is tested and **on hold** (section 17). Until it runs: no ITD uploads for setups 217–220, no `itd_seed_positions.py --apply`, no migration 39, no manual table changes.

### 8.10 Calendar and reminders
- Carrier **Bordereau Calendar** (`/bordereau-calendar`) and broker **Broker Calendar** (`/broker/calendar`). For the broker, "done" means processed. Tiles: Due / Sent on time / Sent late / Never sent. Every version of a file is viewable.
- Brokers pick the real reporting period from the calendar. Late-broker reminders are emailed with the carrier cc'd.
- Known small bugs (verify): the "N new files" banner over-counts; a bare year in a file name reads as January; French month names are not recognised.

## 9. Dashboards, audit, notifications

- **Dashboards** per seat show only what that seat needs. Kavachio: seats, carriers, brokers, Data Mapping Queue, Rule Library, with carrier and broker filters. Carrier: programmes, files, exceptions, contracts ending within 30 days. Broker: contracts to sign, bordereaux due (overdue months named), exceptions to review, recently processed files.
- Charts: ranked horizontal bars, validated palette, never dual-axis. Tiles get no helper text. Chart cards get an ⓘ.
- **Audit Logs** (`/audit`, every seat). `audit_feed.scope_for` is the **one** visibility rule: a broker sees their firm, a carrier sees its own seats and its brokers, Kavachio sees everything. The audit endpoints have no role guard on purpose, so any new audit endpoint must scope through `scope_for`.
  - **Since 6 Oct, carriers see the broker *person* by name** (e.g. "Wani", role "Broker · Wani org"). Kavachio's own steps read "Kavachio · Automatic".
  - Targets are shown by name, not id (`audit_names.py`). Decisions are stored one row per decision and **shown grouped per save** ("View all N changes"); the download stays one line per decision.
  - Bookkeeping rows (data-model saves, unattended runs) show to Kavachio only (`PLATFORM_ONLY_ACTIONS`). 200 rows per page.
  - New action names go in `audit._FRIENDLY` (read on the way out too). Check keys against `/openapi.json`.
- **Notification bell:** reminders such as "Setup needed — file waiting". Platform notices go to Kavachio admins (`notifications.py`).
- **Emails** go out as "Bordereau Management" through the `NOTIFY` SMTP account. A rejected file sends one email, and "Sender Notified" appears only when an email really went.

## 10. Screens (routes in `frontend/src/App.tsx`)

| Area | Routes |
|---|---|
| Public | `/login`, `/reset` (also invite accept), `/invitations` (broker joins another carrier), `/sign` (e-sign), `/fix/:token` (secure link) |
| Kavachio | `/admin/dashboard`, `/tenants` (carriers, Onboard Carrier), `/admin/users`, `/admin/mapping-tasks` (Data Mapping Queue), `/direct` (process a file) |
| Carrier | `/home`, `/programs`, `/program-management`, `/programs/new`, `/brokers` (+ `/requests`, `/:id`, `/:id/files`), `/parties` (my brokers), `/contracts` (+ `/new`, `/:id`, `/signatures`), `/direct/setups` (Bordereau Setup) + `/direct/setup`, `/outputs/templates/:id`, `/files` (Files Received + Ingestion Channels), `/bordereau-calendar`, `/calendar`, `/runs` (File Submissions), `/rule-library`, `/users`, `/tenant` (Company), `/profile` |
| Broker | `/broker` (dashboard), `/broker/contracts`, `/broker/bordereau` (Process Bordereau), `/broker/runs`, `/broker/calendar`, `/broker/exceptions`; `/broker/team-activity` and `/broker/files-by-carrier` still exist, unlinked |
| Shared | `/uploads/:id/exceptions` (+ `/rule/:ruleId`), `/audit`, `/welcome` |

`/journey` (a 3D scroll story page) exists **only on one dev PC**. It is excluded locally and is not in git (section 13).

## 11. Data model and migrations

- Live schema = canonical **v4** model (`data_model.py`, `canonical.py`, from `Kavachio_Data_Model_Spec_v4.xlsx`). Tables use entity-prefixed column names (`user_email`, `contract_program_id`); the ORM and `align_to_database` handle it.
- `data model docx/data-model.png` (next to the repo) is the **target** model (Superset v3.1, 87 tables), not the live one. Names differ: insured_location = `risk_location`, tax_or_surcharge = `tax_line`, commission = `commission_line`. The diagram has no e-sign, output template, carrier_broker or program_broker tables. `LEGACY_FIELD_MAP` maps old names to live ones.
- Migrations: plain SQL in `backend/python-services/migrations/` (10_2 … **39**; 36 is missing). `db.py` also adds missing columns at start-up. **A person runs migrations on the shared DB, deliberately, never a test.** Re-check the live DB before assuming one is applied.
- ⚠ **Next migration numbers:** **40 is reserved for the ITD repair** (`repair_40.sql`). The reconciliation plan (Task 23) moves to **41** (proposed 16 Oct). Confirm with the team before taking a number.
- SCD-2 versioning exists for **contracts and policy records only**.
- `app_user` role/scope checks were restored as NOT VALID (migration 22). A DB trigger requires every login except the first Kavachio account to record who invited it.
- `activity_events` has `actor_user_id`, `actor_role`, `actor_broker_party_id` (since 24 Sep; older rows not backfilled).
- **Reuse existing tables before adding new ones.** The team has pushed back on several new tables per feature; justify each one.

## 12. House rules and conventions

- **Wording:** plain words, short, no jargon. Explanations go behind the **ⓘ** icon (`InfoTip`, `StatCard info`), not grey lines under labels. List cards carry only the choice. Say "Carrier" / "Broker", "Active" (not "In force"). Dialogs use the app's own popups.
- **Show proposed on-screen wording for approval before applying it.**
- **No code changes without an explicit "yes"** when analysis or a plan was asked for. Propose, then wait. Lead every report with a one-line plain-words verdict.
- **Pagination:** backend paging is opt-in (`page` / `page_size`; without `page` the response stays the same). Add an `xPaged()` client wrapper rather than changing the old one. `<Pagination/>` only works on `.proto` pages; Tailwind pages use the raw button pager (BrokerDetail is the template). Filters computed per row must run **before** the page is cut.
- **Scope:** broker A must never see broker B's data. New broker-facing routes must check the broker's programmes (`carrier_scope`).
- Prefer one code path behind a switch over two copies.
- Commit messages: short bullets that a non-technical reader understands.
- Branches: `development` (default, where work lands), `main`, `qa`, `development-phase-2`. GitHub shows `qa` and `dev` deployment environments.
- Don't touch another developer's in-flight work or staged files without asking.
- Never paste passwords, keys or personal emails into chat, commits or this file.

## 13. Gotchas (each one has cost someone a day)

- **The dev server auto-reloads against the shared DB** (192.168.2.11). Saving `db.py` runs table changes there. Other developers' servers (e.g. 192.168.2.21, .143) use the same DB.
- A slow boot is usually a **Postgres lock**, not the app. Check `pg_stat_activity` for an idle-in-transaction session before blaming code.
- **DB times are UTC** even though the DB timezone is Asia/Kolkata. Compare in UTC.
- **Tests that write leak into the shared DB.** It has happened four times (17, 21, 25 Sep, 14 Oct; 41 test carriers on 14 Oct). Never run DB tests with the shared DB in `.env`.
- `TRUNCATE party … CASCADE` also empties `app_user`. A keep-list does not protect a table from cascade.
- `pipeline_decisions.log` is **tracked in git even though `.gitignore` lists it**, so it shows as modified after every run and has been committed many times. Leave it out of commits (the team can untrack it with `git rm --cached`).
- **`App.tsx` may be marked skip-worktree on a dev PC** (to hide the local `/journey` route). Git then ignores your edits to it. Check with `git ls-files -v | grep '^S'` and undo with `git update-index --no-skip-worktree frontend/src/App.tsx`.
- Claude worktree runs leave `worktree-wf_*` branches and `.claude/worktrees/` folders. Clean up with `git worktree remove` and `git branch -D`.
- A failed AI library bind can be **cached as a failure**, sending later setups' library rules to review (verify fix). The Gemini key in `.env` was denied (403) on 29 Sep (verify).
- **JWT_SECRET is unset on the dev server**, so logins, fix links and e-sign use the committed dev secret.
- `.xls` (old Excel) is accepted at the door, but `xlrd` is not in `requirements.txt` (verify it can be read).

## 14. Testing

- **Unit tests:** `backend/python-services/test_*.py` (pytest). On the main dev PC, run them with the venv interpreter and `~/.local` pytest **appended** to `sys.path`:
  `./.venv/bin/python -c "import sys; sys.path.append('$HOME/.local/lib/python3.10/site-packages'); import pytest; sys.exit(pytest.main(sys.argv[1:]))" test_x.py -q`
- **Known failing:** 3 tests in `test_autorun_no_setup.py` since `df20924`. Their stand-in arrival lacks `reporting_period`; it is a test gap, not a product bug. A one-line fix is prepared, not applied.
- **Frontend:** `npx tsc --noEmit` (Node 22).
- **Flows that write:** use a disposable local Postgres copy (pg_dump of the shared DB restored locally, API on another port). Never use `:8000` or the shared DB.
- **Read-only proof on the shared DB:** set `PGOPTIONS="-c default_transaction_read_only=on"` before importing `db`, stub storage, and block sockets.
- After an approved build, run the **requester's own demo file** through the same screen and route they use, and check the downloaded Excel.
- Test data on the shared DB: carrier "Mahi Corp pvt ltd" (tenant 1377) with broker "Wani org" (party 1600); contract 7529 (uploaded, expired 1 Oct) for renewal tests; contract 7758 (shared broker); ITD test tenant 6, setups 217–220.

## 15. Where to find things

| Need | Look at |
|---|---|
| Roles, scope, retired seats | `auth_deps.py`, `carrier_scope.py`, `frontend/src/access.ts`, `auth.ts` |
| Carrier → programme → broker | `hierarchy_routes.py`, `carrier_routes.py`, `broker_onboarding_routes.py`, `segment_routes.py` |
| Contract record, renewal, period | `contract_routes.py`, `contract_types.py`, `renewal_chain.py`, `period_change.py`, `ContractRecord.tsx`, `ChangePeriodDialog.tsx` |
| Clause/rule AI | `contract_upload_services/` (extraction, `validation_rule_generator.py`, `rule_compiler.py`, `generic_rule_library.py`, `variation_*`), `ai_cache.py` |
| E-sign | `esign_*.py`, `SignContract.tsx` |
| Bordereau Setup and run | `direct_routes.py`, `direct_mapper.py`, `direct_lane.py`, `direct_render.py`, `rule_scope.py`, `DirectSetup.tsx`, `BordereauSetupEdit.tsx` |
| Output templates | `output_template_*.py`, `reporting_standards.py`, `bordereau_template.py`, `OutputTemplate.tsx` |
| Checks | `duckdb_validation.py`, `validation_routes.py`, `validation_outcome.py`, `contract_margin.py` |
| Intake | `intake_*.py`, `email_*`, `imap_idle.py`, `sftp_*.py`, `FilesReceived.tsx`, `IngestionPanel.tsx` |
| ITD | `itd.py`, `itd_store.py`, `itd_run.py`, `itd_routes.py`, `ItdComparisonDrawer.tsx` |
| Exceptions loop | `submission_service.py`, `submission_routes.py`, `fix_link_routes.py`, `export_results.py`, `UploadExceptions.tsx`, `FixLink.tsx` |
| Calendar | `submission_calendar*.py`, `sweep_scheduler.py`, `BordereauCalendar.tsx`, `BrokerCalendar.tsx` |
| Audit | `audit.py`, `audit_feed.py`, `audit_names.py`, `decision_log.py`, `AuditLogs.tsx` |
| Data model | `data_model.py`, `canonical.py`, `ingester.py`, `db.py` |
| Original mapping engine and storage docs | `backend/python-services/README.md`, `README-storage.md` |

## 16. Decided out of scope or on hold (don't build without asking)

From the team's Build Status reviews (verify before relying):
- **Out:** reinsurance and treaty features in Phase 1 (ceding, cessions, treaties), tax calculation, claims bordereaux, XML/JSON output layouts and API delivery, Binder Details & Sections, sanctions or licence re-screening on renewal, reminder emails before a contract ends, file-intake channel fallback, "an empty mandatory column stops the file".
- **On hold:** Policy Lifecycle, month/period financial totals, margin by currency, the "policy acts as one" extension.
- The previous-submission reconciliation (23.1/23.2) and the Change period "Endorsement" kind are **plans only**.

## 17. In flight and open points

Keep this short and current. Remove an item when it ships or is dropped.

- **ITD table repair (Migration 39):** tested on a copy (39/39) and dry-run on the shared DB. **The real repair is on hold** pending team sign-off. See `Migration_39_team_note.md` and the investigation next to the repo. The owners of 192.168.2.21 and .143 were asked to confirm they run `df20924` or later.
- **Treaty / reinsurer-sharing code exists outside this repo.** On 14 Oct it created 7 tables on the shared DB (`reinsurance_treaty`, `_layer`, `_programme`, `_share`, `reinsurer_access_link`, `reinsurer_access_event`, `treaty_terms`) and contract 7836 under carrier 588. It appears to run on 192.168.2.21 (verify). Whoever owns it, please push it or say so.
- **14 Oct test data** on the shared DB (41 test carriers): a cleanup script is prepared and rehearsed, **not run**, awaiting approval.
- **Process Bordereau reads the file for the column check before the safety checks** (`direct_routes.py`, `check_layout` before `_record_manual_arrival`). Raised 9 Oct, not fixed.
- **Alias renames give 0 or blank premium** (e.g. "GWP"): the door accepts the alias, but the run looks up the exact name. Owner decision pending (verify).
- **Old untied output layouts** (57 found on 12 Oct): the backfill dry run passed and the SQL is proposed, **not run**.
- **Policy children wipe (latent):** loading a policy can retire earlier rows by policy id alone. Fix awaiting approval (verify).
- **Quick-fixes patch (8 Oct):** never applied and now stale, since parts were done separately. Rebuild before use.
- **Carrier notification emails** may go to nobody (`notifications.py` uses the old `tenant_admin` role name) (verify).
- **Previous-submission reconciliation:** plan only (`Kavachio_Previous_Submission_Reconciliation_Plan.md`).
- **Ownership transfer** has no target now that carrier users are retired (open product point).
- **Rotate `JWT_SECRET`** before any real deployment.

## 18. Reference documents next to the repo (not in git; ask the team for copies)

- `phase documents/`: `Kavachio_Feature_List.docx` (the scope checklist since 31 Aug: 3 phases, ~95 features), plus newer `Kavachio_Three_Phase_Plan_v4.docx`, `Consolidated Feature List.docx`, `Open_Questions_Register_v3.docx` (14 Oct; verify which is current). Reconciliation follows the **Phase 1 Google Sheet** ("Kavachio Carrier Plan"); sub-task numbers like 19.1–23.2 come from it.
- Plans: `Kavachio_ITD_BDX_Implementation_Plan.md`, `Kavachio_Previous_Submission_Reconciliation_Plan.md`, `change-period-plan.md` (v7), `Kavachio_Output_BDX_Implementation_Plan.txt`, `Migration_39_ITD_Table_Rename_Investigation.md`.
- Design: `figma design/` (mockups and HTML prototypes), `flow-diagrams/` (structure, flow, role features, email/API intake), `data model docx/`.
- Demo and test files: `demo 13 aug/`, `testing contract & BDX/`, `New claim and premium code/` (the original offline ITD algorithm).

## 19. Team (git authors)

- **DineshC1994:** contracts, renewal, Bordereau Setup, dashboards, audit, intake emails and SFTP, most UI wording.
- **sachinargade01:** intake pipeline, ITD, submission calendar, Prior Period Files, setup UI.
- **vaishnavivishwasrao:** initial broker codebase, contract create flow, multi-carrier flow.
- Also seen: `dinesh-choudhary-aspl`, `kavachio-softwares`. Sister repo: `kavachio-softwares/kavachio` (the earlier broker product; some features were ported from it). Fixes there do not reach here.

## 20. Change log (newest first)

One plain line per pushed change: **date · commit · author · what it does for the user**. `git log` has the full history.

| Date | Commit | Author | Change |
|---|---|---|---|
| 2026-10-16 | `53065df` | DineshC1994 | Renewals can't overlap the previous term (gaps confirmed once); uploaded renewals keep a "Make it active" card; Change period date correction behind a switch, with evidence, undo and a broker email; a broker shared between carriers can hold contracts with each; all shown in Audit Logs. |
| 2026-10-14 | `0be18f1` | DineshC1994 | Renewal end to end (copy terms, 30-day window, supersede on activation, signed PDF for uploaded renewals, files routed across the chain or held for a person to pick); Documents panel text shortened. |
| 2026-10-14 | `702ba8d` | sachinargade01 | Prior Period Files: a new contract version closes the previous one's end date at the new start. |
| 2026-10-13 | `df20924` | sachinargade01 | **ITD bordereaux:** only new and changed rows go to the carrier; comparison screen; migrations 38–39. |
| 2026-10-13 | `5e6e0d4` | DineshC1994 | Bordereau Setup lists only programmes with a broker that has a contract; shorter upload messages. |
| 2026-10-13 | `e8671a1` | DineshC1994 | Fix & Validate saves a new version; Results tab/sheet/CSV; one margin per contract; policy-total limits; secure link works for hand-uploaded files. |
| 2026-10-12 | `c8804ce` | DineshC1994 | Output templates saved per programme + broker + contract. |
| 2026-10-09 | `cd945a8` | DineshC1994 | Kavachio's own SFTP server with emailed logins; every setup and contract needs a broker. |
| 2026-10-09 | `02bbe48` | DineshC1994 | Broker emails for email/SFTP/API files only; one email per rejected file; start-up line shows email switches. |
| 2026-10-09 | `625ff6b` | DineshC1994 | Files sent before a setup exists are kept and run once it is live. |
| 2026-10-09 | `f49c9aa` | DineshC1994 | 90% column-match popup before a broker run; missing/extra columns after. |
| 2026-10-09 | `1263424` | DineshC1994 | Carriers can remove a broker from a programme. |
| 2026-10-07 | `c0895fc`, `9b2b3de` | DineshC1994 | Contract Rules tabs reorganised; Audit Logs name the real person, group bulk saves. |
| 2026-10-06 | `ea4b037`, `e24de93`, `f180e41` | DineshC1994 | Uploaded-contract renewal asks for the signed PDF; Audit Logs show names not ids; clearer versions and calendars. |
| 2026-10-05 | `0eb5f48`, `39b3d5c`, `56131c3`, `4a2d9cb` | DineshC1994 | SFTP pull, email to Kavachio's mailbox, secure fix links, every file names programme/contract/month, versions per month. |
| 2026-10-01 | `1c7bcbd`, `c83e517`, `4a6c259` | DineshC1994 | Intake files kept until processed; rows outside contract versions checked; Ingestion Panel tabs. |
| 2026-09-30 | `211156f`, `0e7b6b8`, `c497529` | DineshC1994 | Files Received vocabulary; "Carrier"/"Broker" labels; 30-day contract-ending warnings; contract reuse per broker. |
| 2026-09-29 | `f409b9f`, `a59a870`, `f057151`, `8fae0d1` and others | DineshC1994 | Carrier users and operators retired; approvals behind a switch; broker calendar; Rule Library loader fixed. |
| 2026-09-29 | `8c813b4` | sachinargade01 | Ingestion UI and automatic file processing. |
| 2026-09-28 | `58fb656`, `6fcec0c`, `56e6db3` | DineshC1994 | Broker onboarding approval; contract page tabs with whose-turn line. |
| 2026-09-24 | `da817c0`, `db27eaf`, `4dd00bb` | DineshC1994 | Audit Logs screen; highlight-column fix; setup in 3 steps; carriers view-only on exceptions. |
| 2026-09-22 | `07ea0e7`, `e3e8179`, `d92b584` | DineshC1994 | Dashboards rebuilt; seats separated; permanent exception decision record. |
| 2026-09-16 | `f18ad32` | DineshC1994 | Sparse templates no longer look clean; same file gives same result; rule sets per output template. |
| 2026-09-15 | `a6c937f`, `9f576e0` | DineshC1994 | Paged lists; wording top-up; AI double-checks column matches. |
| 2026-09-11 | several | DineshC1994 / sachinargade01 / vaishnavivishwasrao | 100-word contract minimum, blank bordereau template, per-contract BDX template, SFTP/email pickup, contract upload popup. |
| 2026-09-08 | `6b975ba`, `0728b43`, `496d8c0` | DineshC1994 / sachinargade01 | E-signing, contract types, Prior Period Files, multiple active contract versions. |
| 2026-09-01–07 | several | team | Carrier-centric flow, broker sign-in, intake pipeline (API/SFTP/email), output template builder, submission calendar. |
| 2026-08-28 | `e6510fe` | vaishnavivishwasrao | Initial commit and broker codebase. |

---

## How to keep this file current

**Whoever pushes updates this file in the same commit.** Claude sessions: do this before saying the work is done.

1. **Add one row** at the top of the change log (section 20): date, short commit hash (or "next"), author, one plain sentence about what changed *for the user*.
2. **Edit the section the change touches.** Change the sentence; don't append a contradicting one. If a rule changed, delete the old rule.
3. New switch → section 4. New screen or route → section 10. New migration → section 11. New gotcha → section 13. Something left open → section 17. Something decided out of scope → section 16.
4. **Before starting work:** read sections 2, 5, 12, 13, 17 and the section for your area, then run `git log --oneline -15` to see what others pushed since the "Last full review" date. Don't repeat or undo their work.
5. Keep it **short and plain**. Facts only; never write a plan as if it were built. Mark unsure items **(verify)**.
6. Update "Last full review" only after re-checking the whole file against the code.
