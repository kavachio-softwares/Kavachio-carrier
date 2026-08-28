# Kavachio Frontend — UX Text Audit

Audit of all user-visible text across 29 pages + shared components (audience:
insurance ops staff at MGAs/brokers — business users, not developers).
Each finding: location → current text → problem → suggested wording.

---

## Part 1 — Systemic problems (fix once, centrally)

### 1.1 Raw technical errors shown to users (~25 places) — HIGHEST PRIORITY

The pattern `e?.response?.data?.detail ?? e?.message ?? "X failed."` puts raw
backend/axios text on screen ("Request failed with status code 500",
"Network Error", FastAPI internals). Worse: when `detail` is a FastAPI
validation **array/object**, unguarded spots crash the React render, and
`DirectSetup.errText()` even `JSON.stringify`s it into a banner.

Locations:
- `pages/Login.tsx:40`, `pages/ResetPassword.tsx:53`, `pages/Profile.tsx:57,75`
- `pages/Welcome.tsx:83,102,121`, `pages/AddUser.tsx:23`
- `pages/Tenant.tsx:46,62`, `pages/AddTenant.tsx:46` (no string guard — array crash risk)
- `pages/PartyDetail.tsx:76,106`, `pages/AddParty.tsx:40`, `pages/AddProgram.tsx:36`
- `pages/Programs.tsx:174,189`
- `pages/Uploads.tsx:169,216,235`, `pages/UploadExceptions.tsx:57,69,91`
- `pages/Mapping.tsx:184`, `pages/AdminMappingTasks.tsx:48`
- `pages/Outputs.tsx:235,325,421,472,506,1398`
- `pages/OutputTemplate.tsx:133,148`
- `pages/DirectSetup.tsx:2663-2667` (`errText` helper — JSON.stringify + e.message)
- `pages/DirectRun.tsx:135`, `pages/RuleReview.tsx:37`
- `components/ExceptionDecisionTable.tsx:595,605-608,660`

**Fix once:** add a `friendlyError(e, fallback)` helper in `api/client.ts` that:
only shows `detail` when it's a known-safe string; maps network errors to
"We couldn't reach the server. Check your connection and try again."; and
otherwise returns the caller's friendly fallback (always with a recovery
action, e.g. "We couldn't save your changes. Please try again.").
Note: `RuleReview.tsx` and `UploadExceptions.tsx` read `data.message` while
everything else reads `data.detail` — one of them is always undefined.

### 1.2 Silent failures & misleading empty states (~20 places)

Fetch errors are swallowed (`.catch(() => setX([]))` or no catch at all), so a
network failure renders "No data yet" — users can't tell "empty" from
"broken", and some saves fail invisibly (user believes config was saved).

- Error shown as empty state: `Home.tsx:69` ("No runs yet"), `Users.tsx:59`,
  `Tenants.tsx:55` ("No tenants found."), `TenantDetail.tsx:84-100` (all 5 fetches),
  `Parties.tsx:54`, `PartyDetail.tsx:58`, `AdminMappingTasks.tsx:41`
  ("all known formats are mapped" — actively wrong), `RecentRuns.tsx:65`,
  `Uploads.tsx:244` ("No canonical data found"), `Outputs.tsx:261` ("No rows in this file."),
  `DirectRun.tsx:88,102` (failure shown as "No program for this carrier" / "No active setup")
- No catch at all (spinner ends, nothing happens): `Home.tsx:49`, `Welcome.tsx:52-62`,
  `Users.tsx:93` (delete), `Tenant.tsx:30,66` (eternal "Loading…"),
  `Parties.tsx:60-66` (toggleActive), `Programs.tsx` (6 handlers: 60-74, 77-90,
  106-127, 132, 141-146, 193-207), `AddProgram.tsx:20-22`, `Uploads.tsx:81-98,116-132,207`
- **Saves that fail silently (most dangerous):** `SheetBindings.tsx:57-67`,
  `DirectSetup.tsx:308-317,326-333,334-340` (removeSupplement even clears the UI
  *before* the request, so a failed delete looks successful), `Outputs.tsx:295-297`
  (pre-generate check vanishes with only console.warn)

**Fix:** every fetch needs a distinct error state ("We couldn't load X —
refresh to try again.") separate from the true empty state; every save needs a
catch that tells the user it didn't save.

### 1.3 Errors rendered in success/neutral styling

- `Mapping.tsx:184→231` — save errors render in the **green** `note ok` banner
- `OutputTemplate.tsx:133→199` — save failure in green `text-emerald-700`
- `Outputs.tsx:776` — errors in muted grey `msg`, and only visible on one tab

### 1.4 Native alert()/confirm() dialogs (9 places)

Jarring, unstyled, unbrandable — and the app already has a good `Modal`.
- `Users.tsx:89` (alert), `Users.tsx:92` (confirm — no consequence explained,
  inconsistent with the polished reset-password modal on the same page)
- `Uploads.tsx:603,835` (alert), `UploadExceptions.tsx:159` (alert),
  `Outputs.tsx:252` (alert), `ExceptionCards.tsx:285` (alert — and the endpoint
  doesn't exist yet, so users hit it every time; hide the button until it ships)
- `OutputTemplate.tsx:139` (confirm), `DirectSetup.tsx:957,978,2332` (3× window.confirm)

### 1.5 Internal jargon leaking into UI (vocabulary sweep)

| Internal word | Where it leaks | Say instead |
|---|---|---|
| **tenant** | Tenants.tsx:89,102,106,160,162,168; TenantDetail.tsx:106,170; AddTenant.tsx:46,53,66,72,125,137; Home.tsx:105; Users.tsx:18,129; Mapping.tsx:259; AdminMappingTasks.tsx:191; DirectRun.tsx:178-183 | organization / "your organization's administrator" |
| **party** | Parties.tsx:90,99,124,161,166; PartyDetail.tsx:146 etc.; AddParty.tsx:40; Programs.tsx:221,225,231,233,239,254,261,275,302,535; Welcome.tsx:121; Uploads.tsx (party dropdowns) | trading partner (the directory page is already titled "Trading Partners") |
| **canonical** | Uploads.tsx:384,448,577,785; Mapping.tsx:252; Outputs.tsx:523,1707; OutputTemplate.tsx:194,453; AdminMappingTasks.tsx:120 | "Kavachio field" / "processed data" |
| **ingest/ingested** | Uploads.tsx:193,202,258,460,513,526-528,575,581 | process / load |
| **mapper** | Uploads.tsx:214,676-682,582; Mapping.tsx:176,200 | template / mapping |
| **token** | ResetPassword.tsx:126 | "the full link from your email" |
| **backfill** | Mapping.tsx:218; OutputTemplate.tsx:206-210 | "process the waiting files" |
| **bind/bound** | Programs.tsx:401; DirectSetup.tsx:819; SheetBindings.tsx:71,89 | assign / match ("bind" also collides with insurance "bind") |
| **SCD-2 lecture** | Outputs.tsx:1414-1419 explains SCD-2 versioning to ops staff | "We keep a full history of previous values — nothing is ever deleted." |
| **ops** | DirectSetup.tsx:599,965,1012 | "your team" |
| **unsourced / last wins / row strategy / enum / pattern / similarity** | DirectSetup.tsx:957,1693; OutputTemplate.tsx:339,562-585,478 | plain language (see per-file section) |

### 1.6 Raw enum/ID/JSON values shown as labels

- Lowercase party types (`carrier`, `mgu`, `tpa`) as visible options/pills:
  Welcome.tsx:280, Programs.tsx:236,245,264,297, Uploads.tsx:44,281,290,313
- Raw statuses (`draft`, `superseded`, `active`): TenantDetail.tsx:51,
  PartyDetail.tsx:237, Programs.tsx:297, DirectSetup.tsx:1786,2248, Outputs.tsx:1041,1084
- Raw field keys as form labels — Welcome.tsx:226,231 renders **line1, line2,
  city, state, zip, country** literally
- snake_case entity names as tabs/options: Uploads.tsx:774
  (`premium_transaction`…), OutputTemplate.tsx:339-342, Outputs.tsx:750 (`policy_ids`)
- Programming types as transform options: OutputTemplate.tsx:619 (`int`, `str`)
- Raw JSON in cells: Uploads.tsx:736, DirectSetup.tsx:2255
- Internal IDs as headlines: UploadExceptions.tsx:139 (`EXPORT-12`),
  RuleReview.tsx:57 + UploadExceptions.tsx:240 (raw `ruleKey` like
  `no_rule_policy.premium_amount`), DirectSetup.tsx:590,1445,1786, Mapping.tsx:200
- Raw SQL shown to business users: Outputs.tsx:1496-1517 ("View SQL")
- Rule-engine codes as pills: OutputTemplate.tsx:534 (`AJV`, `IR_V1`)

### 1.7 Inconsistent naming & casing

- **One feature, four names:** "Data Mapping Queue" (Layout), "Data-model
  queue" (Home:175), "Mapping tasks" (Home:173), "Open map tasks" (admin
  dashboard:248) → standardize on "Data Mapping Queue"
- **Same action, different labels:** "Save program" (PartyDetail:302) vs
  "Confirm program" (Programs:388); "Save Draft / Approve & Save" (Mapping:223)
  vs "Save draft / Save & activate" (OutputTemplate:177); "Choose output
  field…" (DirectSetup:2616) vs "Select output field…" (ContractDetail:387)
- **Title Case vs sentence case:** "All Types/All Statuses" (Tenants:116,121)
  vs "All roles/All statuses" (TenantDetail:194); "Programs & Contracts"
  (Programs:220) vs "Programs & contracts" (TenantDetail:179); "Organization
  Name" (Tenant:125) vs "Organization name" (AddTenant:87); SheetBindings
  Title-Cases everything ("Back To Mapping", "Save Bindings", "No Schedule
  Sheets"); Mapping.tsx badges/placeholders ("To Confirm", "Search Columns…");
  AdminMappingTasks:143,158 ("How To Map", "Review & Map The Columns");
  Outputs:1284,1289 vs 701/989 → pick sentence case app-wide
- **"(s)" plural hacks** (~12 strings): Uploads:202, Outputs:317-321,681,961,
  1234,1296-1298,1490,1495, DirectSetup:819,957,1303,1368 → compute plurals
- "clean" (Outputs:1041) vs "Clean" (RecentRuns:186); "Decided" vs "decided"
  (ExceptionDecisionTable:715,892)
- Emoji in native controls render inconsistently: "➕ Create new party…"
  (Uploads:284,396, Programs:239, DirectSetup:1050,1069), "✨ Set Up Kavachio
  Mapping" (AdminMappingTasks:221) → plain "+" / no emoji
- Brand lowercase: "kavachio mapping" (AdminMappingTasks:114)

---

## Part 2 — Notable per-file findings (beyond the patterns above)

### Login.tsx / ResetPassword.tsx
- `Login.tsx:146` "You'll land on the right home screen for your role." — remove or "Sign in to access your workspace."
- `ResetPassword.tsx:126` "…missing its token" → "This reset link looks incomplete. Please open the full link from your email, or request a new one."
- Otherwise the strongest files in the app (good expiry notes, recovery paths).

### Profile.tsx
- `:55` "Saved" → "Your name has been updated." (password flow uses a full sentence)
- `:75` map the common case explicitly: "Your current password is incorrect."
- `:104` "can't be changed here" → "To change it, contact your administrator."

### Welcome.tsx (onboarding wizard — worst first impression)
- `:226,231` raw keys as labels (**line1/zip/country**) → "Address line 1", "ZIP / postal code", "Country"
- `:141` raw account code shown as "Organization" while `:174` calls it "Account code" — show legal name, one label
- `:174` "Account code (read-only)" → hint "Assigned by Kavachio — can't be changed."
- `:283-288` "Scope: My records / Kavachio global" → "Visibility: Only my organization / Shared Kavachio directory"
- `:171` ALL-CAPS types (CARRIER) next to acronyms; list also disagrees with the admin page's allowed types

### Home.tsx
- `:29` "What needs you today" → "What needs your attention today"
- `:226` button says "Download →" but the click opens the exceptions screen — label lies; use "Open →" or actually download
- `:184` "file → validated output" → "from upload to validated output"

### Users.tsx / AddUser.tsx
- `Users.tsx:213` "Role labels match the invite dialog exactly." — dev/QA note shown to users; delete
- `AddUser.tsx:91` success says "User created" but the action was an invite → "Invite sent"
- `AddUser.tsx:71` "labels match the Users table" — dev note; delete

### Layout.tsx
- `:255` **"v0.9 · POC build"** shown to paying customers — undermines trust → "v0.9" or "Early access"

### Tenants/AddTenant/TenantDetail
- Full tenant→organization rename (headings, buttons, empty states, success
  modal "Tenant created", loading overlay, pagination noun)
- `TenantDetail.tsx` has **no loading states** — empty-state strings flash before data

### Parties/PartyDetail/AddParty/Programs
- Unify on "trading partner" everywhere (page title already says it)
- `Parties.tsx:148` bare word "Kavachio" in the actions column → "Managed by Kavachio" + tooltip
- `AddParty.tsx:35-37` if the partner saves but the contact fails, the error
  says "Could not create party" and a retry duplicates the partner → split the
  calls and messages
- `AddParty.tsx:95` Tax ID placeholder `**-*******` reads as masked → "e.g. 12-3456789"
- `Programs.tsx:311` "New program from contract" creates an empty draft — no
  contract involved → "Create program"
- `Programs.tsx:423` activation errors render under a hardcoded "Upload failed." header
- `Programs.tsx:487` "Failed" pill with no explanation or action → "Extraction failed" + hint
- `Programs.tsx:323` "AI · from {file}" → "Auto-filled from {file}"
- `AddProgram.tsx:46` "what a Setup and contract are scoped to" → "Its contract and bordereau setup are added in the next step."
- `AddProgram.tsx:49` "← Carrier" links to a partner that may not be a carrier → "← Back to partner"
- `AddProgram.tsx:7` offers `Active/Draft/Inactive` (capitalized values) while Programs/PartyDetail send lowercase `draft/active` — status values disagree across pages

### Uploads.tsx
- Jargon sweep: ingest→process, mapper→template, canonical→processed (see 1.5)
- `:214` "No saved mapper for this format. Switch to Sample mode…" → "We don't recognise this file's format yet. Use Sample (format only) first to set up a template."
- `:857` "Review / change" vs "Open & approve" — two phrasings for one action

### UploadExceptions.tsx
- `:164-170` the same label "Fix & re-run" performs two different actions
  (re-validate vs re-generate) depending on context → "Re-validate upload" /
  "Apply fixes & re-generate output"

### Mapping.tsx / AdminMappingTasks.tsx
- `Mapping.tsx:383` "All Kavachio Data-Model Field" — grammar (singular) → "All Kavachio data fields"
- `Mapping.tsx:291` "n/a" → "No suggestion"; `:221` "← Queue" label is wrong when opened from /uploads
- `AdminMappingTasks.tsx:81,199` "Format #—" when id is null → "Unnamed format"

### Outputs.tsx (largest page, most findings)
- `:1205` "Cancelled." lingers as grey text → drop or "Generation cancelled — no file was created."
- `:1248` fallback renders `policy.premium_amount = -200` → "Field {field} has value {value}, which breaks this rule."
- `:1303,1311` "(no document name detected)" / "(unnamed reference)" → "Document name not identified"
- `:1455-1495` correction results show raw `table.column`, "rule ? · field → value",
  "(multiple matching rows — used the first)" → human sentences ("More than one
  record matched — we updated the first one. Please double-check this policy.")
- `:759` asks for internal policy IDs with no example → placeholder "e.g. 1043, 1044"
- `:1033,1180` raw stage values `input/output` → "Upload check" / "Output check"

### OutputTemplate.tsx
- `:206-210` "predates the ranked-candidates feature… backfill the top-10
  candidates" — internal feature history → "This template doesn't have AI
  suggestions yet. Click Re-run AI mapping…"
- `:711` renders "—%" when confidence is 0 → "—"

### DirectSetup.tsx / DirectRun.tsx
- `DirectSetup.tsx:665,690,839,857` step text + error concatenation yields
  "Extracting contract 2/3… failed — Request failed with status code 500" →
  clean phase nouns + friendly reason + "your uploads are still attached" reassurance
- `:1693` "duplicate — last wins" → "This output field is mapped from more than
  one column — only the most recent mapping will be used."
- `:1753` unexplained micro-syntax placeholder "value or @contract:KEY" → hide
  behind an InfoTip
- `:1303` "Some rules were NOT generated" — all-caps shouting
- `:1445` "Output template #4 · Input template #7" — bare DB ids → show names
- `:1028,1939,1984,2034` glyph-only "✕" buttons need aria-labels
- Two cards on one page both titled "Bordereau Setup" (`:1258,1435`) → rename one "Saved setups"
- `DirectRun.tsx:443-446` "a one-time admin task was raised to map it to the
  data model (task #12). Delivery is complete regardless." → "We noticed a new
  file layout. Your output was generated normally; our team will finish
  configuring the new layout — no action needed from you."
- `DirectRun.tsx:171` "No mapping here." — insider aside; `:235` 'there's no
  "create carrier" on this screen' — dev tone

### SheetBindings.tsx
- `:24,116` raw role values `schedule/summary/check/supplement/ignore` as
  dropdown labels → "Schedule", "Summary", "Reconciliation check",
  "Supplementary data", "Ignore this sheet"
- `:143-144` chips "Proposed" / "Set" — "Set" is ambiguous → "Auto-matched" / "Confirmed"

### ExceptionCards.tsx / ExceptionDecisionTable.tsx
- `ExceptionCards.tsx:302-310` "Decisions are held in the browser for now —
  saving is wired to the backend separately." — exposes (outdated) internal
  status and scares users about losing work; delete
- `ExceptionCards.tsx:237` raw lowercase severity vs mapped labels elsewhere → share `SEV_LABEL`
- `ExceptionDecisionTable.tsx:719` "Approve all (recommended)" reads as "this
  button is recommended" → "Approve all recommended values"
- `:716` tooltip "no auto-approvable values… range / relational bounds" →
  "These rows don't have a single recommended value to approve — use Fix to enter one."

### Clean files
`Busy.tsx`, `PipelineStepper.tsx`, `InfoTip.tsx`, `ListFilterBar.tsx`,
`Pagination.tsx`, `components/ui/*` — no issues. `RecentRuns.tsx`,
`ContractDetail.tsx`, `Login.tsx`, `ResetPassword.tsx` are near-clean with
several strings worth copying as house style (e.g. DirectRun.tsx:335
"We couldn't download that file — please try again.").

---

## Suggested fix order

1. **`friendlyError()` helper + adopt everywhere** — kills ~25 raw-error leaks
   and the render-crash risk in one change.
2. **Error states for silent fetches/saves** — the trust-destroying class
   (users believing saves succeeded, or data was lost).
3. **Fix error-in-green styling** (Mapping, OutputTemplate, Outputs).
4. **Replace 9 native alert/confirm with the existing Modal.**
5. **Vocabulary sweep** (tenant→organization, party→trading partner,
   canonical/ingest/mapper→plain language) + enum/status label maps.
6. **Casing + naming standardization** (sentence case, one name per feature/action).
7. Individual copy rewrites from Part 2.
