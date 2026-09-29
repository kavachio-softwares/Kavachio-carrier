// ---------------------------------------------------------------------------
// Route-level access control.
//
// The sidebar hides what a role can't use, but hiding a link is not a guard —
// typing /admin/mapping-tasks (or keeping a bookmark from a previous role) still
// mounted the screen, which then fired requests the backend rejects with 403.
// This module is the single source of truth for "which role may open which
// path"; App.tsx enforces it for EVERY authenticated route (see RequireAccess)
// and Layout.tsx derives the sidebar from it, so a visible link can never lead
// to a bounce and a new screen is covered the moment its path is listed here.
//
// This mirrors the backend's require_role() gates — it is a UX guard, not a
// security boundary. The API remains the authority on every read/write.
// ---------------------------------------------------------------------------

import { matchPath } from "react-router-dom";
import { getTenantBrand, getUser, userRole, type Role } from "./auth";
import { seatOf, type CarrierSeat } from "./hooks/useCarrierSeat";

// The CARRIER side is a superset chain: kavachio_admin ⊇ tenant_admin ⊇
// tenant_user. The two BROKER seats are not part of that chain — they belong to
// a broker organisation, not to a carrier — so they rank below everything and
// pass no carrier gate. Broker sign-in is not wired up yet: the API already
// refuses a broker token on every carrier route ("no tenant bound to this
// user"), and ranking them here keeps the UI from disagreeing with it.
const RANK: Record<Role, number> = {
  operator: -1,
  broker_admin: -1,
  carrier_admin: 1,
  kavachio_admin: 2,
};

// Paths are react-router patterns, matched exactly (`end: true`) so a parent
// entry never silently gates its children — every screen is listed on its own.
// Anything NOT listed needs only a signed-in user, which keeps today's flows
// (Dashboard, Process Bordereau, carriers, programs, outputs, exception triage)
// open to every role exactly as before.
// `requires` is a MINIMUM on the superset chain. `only` is an EXACT set, for
// the few paths where the chain cannot express the rule: broker_admin and
// operator rank the same (neither outranks the other), so a screen that belongs
// to the admin alone has to name it. Same field, same meaning, as Layout's
// sidebar groups.
// `carrierAdminOnly` is the one rule neither field can express. Both carrier
// seats hold the `carrier_admin` role, so `requires`/`only` admit the carrier
// USER as well — right for almost every path, wrong for the few the carrier
// ADMIN alone may open. It narrows the CARRIER side only: a rule that also
// lists broker seats keeps letting them through, because the seat it is about
// does not exist on their side of the fence. Same flag, same meaning, as
// Layout's sidebar items. The server enforces it too
// (carrier_scope.require_carrier_admin) — this only stops the screen mounting.
export const ROUTE_ACCESS: {
  pattern: string; requires: Role; only?: Role[]; carrierAdminOnly?: boolean;
}[] = [
  // --- Platform admin (Kavachio staff, cross-tenant) ---------------------
  { pattern: "/admin/dashboard", requires: "kavachio_admin" },
  { pattern: "/admin/mapping-tasks", requires: "kavachio_admin" },
  // The column-mapping workflow is reached only from the queue above.
  { pattern: "/uploads/mapper/:mapperId", requires: "kavachio_admin" },
  { pattern: "/admin/users", requires: "kavachio_admin" },

  // --- Broker seats --------------------------------------------------------
  // broker_admin and operator share a rank, so both reach these and neither
  // reaches anything above.
  { pattern: "/broker", requires: "broker_admin", only: ["broker_admin"] },
  // No rule for /invitations: it is routed OUTSIDE the app shell (see App.tsx)
  // so RequireAccess never sees it, and a rule here would be config that looks
  // like a guard and enforces nothing. The seat check lives in the component.
  // My Contracts: the broker ADMIN's alone, opened from the dashboard's
  // Active Contracts tile (it has no sidebar entry). An operator never sees a
  // contract at all — same rule as /contracts/:contractId below.
  { pattern: "/broker/contracts", requires: "broker_admin", only: ["broker_admin"] },
  // Running the bordereau is BOTH broker seats' — the operator's day-to-day
  // job, and the admin's when they do it themselves. It was the operator's
  // alone, which read as a division of labour and worked as a blocker: a small
  // broker is often one person holding the admin seat, and a large one still
  // has an admin who covers a month their operator is away. Nothing about the
  // screen is seat-specific — it picks one of THIS broker's contracts and runs
  // against it, and the server already authorises both seats identically
  // (carrier_scope resolves on `is_broker`, never on the role).
  { pattern: "/broker/bordereau", requires: "operator",
    only: ["broker_admin", "operator"] },
  // Run history: both broker seats — the team's runs, whoever sent them.
  { pattern: "/broker/runs", requires: "operator", only: ["broker_admin", "operator"] },
  // The broker's own Bordereau Calendar — what it owes and when. Whoever runs
  // the bordereau needs it, so it follows /broker/bordereau.
  { pattern: "/broker/calendar", requires: "operator", only: ["broker_admin", "operator"] },
  // Every file still carrying exceptions — the dashboard's Exceptions to
  // Review tile, file by file. Both seats, like the triage it opens.
  { pattern: "/broker/exceptions", requires: "operator", only: ["broker_admin", "operator"] },
  // The rest of the dashboard's Team Activity / Files by Carrier cards —
  // broker admin only, same as the cards themselves.
  { pattern: "/broker/team-activity", requires: "broker_admin", only: ["broker_admin"] },
  { pattern: "/broker/team-activity/:userId", requires: "broker_admin", only: ["broker_admin"] },
  { pattern: "/broker/files-by-carrier", requires: "broker_admin", only: ["broker_admin"] },

  // --- Audit Logs ----------------------------------------------------------
  // Every seat, on purpose. It is the one screen whose whole job is to show a
  // person their own accountability, so no seat is excluded — including the
  // operator, who sees only their own trail. The SERVER decides whose rows come
  // back (audit_feed.scope_for); listing every role here only stops the UI
  // bouncing someone before the server is asked.
  { pattern: "/audit", requires: "operator",
    only: ["kavachio_admin", "carrier_admin", "broker_admin", "operator"] },

  // --- Carrier screens -----------------------------------------------------
  // These were unlisted, and an unlisted path falls through to "any signed-in
  // user". Harmless while everyone signing in was a carrier user; the moment a
  // broker could sign in they got the whole carrier app by default. Listing
  // them explicitly is what actually closes that.
  // Process Bordereau — CLOSED to both carrier seats. Sending the bordereau is
  // the broker's half of the month (/broker/bordereau), and a carrier running
  // the file itself is the carrier producing the submission it is meant to be
  // checking. Kavachio staff keep it: they run a file on a carrier's behalf
  // when a broker cannot.
  //
  // This is the one rule, not a hidden link: the sidebar entry and the
  // Dashboard's "＋ Process Bordereaux" button are both derived from it
  // (canAccessPath), so nothing is left offering a screen that would bounce.
  // Everything downstream — Run History, Bordereau Setup, the exception
  // screens — is untouched; the carrier still sees every file and every
  // exception on it, and only cannot start a run.
  { pattern: "/direct", requires: "kavachio_admin", only: ["kavachio_admin"] },
  { pattern: "/parties", requires: "carrier_admin" },
  { pattern: "/parties/:id", requires: "carrier_admin" },
  { pattern: "/programs", requires: "carrier_admin" },
  { pattern: "/programs/new", requires: "carrier_admin" },
  { pattern: "/programs/:programId/setup", requires: "carrier_admin" },
  { pattern: "/programs/:programId/brokers", requires: "carrier_admin" },
  // `only`, not `requires`: a bare `requires` lets kavachio_admin in by rank,
  // and this is the contract record by another route.
  { pattern: "/programs/:programId/contracts/:contractId",
    requires: "carrier_admin", only: ["carrier_admin"] },
  // The contract RECORD screens. The carrier-wide list and the create form are
  // the carrier's — a broker reaches its own contracts through My Contracts.
  { pattern: "/contracts", requires: "carrier_admin", only: ["carrier_admin"] },
  { pattern: "/contracts/new", requires: "carrier_admin", only: ["carrier_admin"] },
  { pattern: "/contracts/upload", requires: "carrier_admin", only: ["carrier_admin"] },
  // The record itself is open to both SIDES, because both have business with
  // it: the carrier decides on it, and the broker has to attach the documents
  // it defers to and correct it after a rejection. The API scopes what each
  // one can see and do — a broker gets a 404 on anyone else's contract.
  //
  // kavachio_admin is deliberately NOT here. A contract is an agreement
  // between a carrier and a broker and Kavachio is not a party to it, so the
  // platform seat sees WHICH contracts a programme has — on the carrier's own
  // page — and not what they say. The server refuses it independently
  // (carrier_scope.assert_can_open_contract); this only stops the UI mounting
  // a screen that would come back 403.
  //
  // On the BROKER side it is the admin's alone. The operator runs bordereaux
  // against a contract but never sees one (same rule as My Contracts above) —
  // agreeing, correcting and signing are the broker admin's.
  { pattern: "/contracts/:contractId", requires: "broker_admin",
    only: ["broker_admin", "carrier_admin"] },
  // Signing is between the two organisations, so both sides reach it. The
  // screen writes nothing, but it names the people who would sign — which is
  // the same reason the platform seat is left off it.
  // On the CARRIER side it is the admin's alone: a carrier user raises a
  // contract, the carrier admin signs it. The broker seats are unaffected —
  // `carrierAdminOnly` narrows only the carrier. The operator is off it for
  // the same reason as the record above.
  { pattern: "/contracts/:contractId/signature", requires: "broker_admin",
    only: ["broker_admin", "carrier_admin"], carrierAdminOnly: true },
  // Party — the carrier's brokers, and where one is invited. `only`, because
  // the platform seat outranks carrier_admin and would otherwise reach the
  // Invite Broker button by typing the path. Inviting a broker is an act in a
  // relationship Kavachio is not part of; the server says so too
  // (carrier_scope.assert_can_invite_brokers). Kavachio's own view of the same
  // ground is the carrier's page under Carriers, which is unchanged.
  { pattern: "/brokers", requires: "carrier_admin", only: ["carrier_admin"] },
  // The broker onboarding queue. `only` for the same reason as /brokers above:
  // approving a request IS sending the invitation, and Kavachio is not part of
  // that relationship (carrier_scope.assert_can_invite_brokers refuses them on
  // the server too).
  //
  // NOT carrierAdminOnly, deliberately, even though only the admin can decide.
  // A carrier user has to be able to read WHY their request was turned down,
  // and they will not be on the programme screen they raised it from when the
  // answer arrives. The server scopes the list to their own requests and
  // refuses them the decisions; the screen simply does not draw the buttons.
  { pattern: "/brokers/requests", requires: "carrier_admin",
    only: ["carrier_admin"] },
  // Create-a-Contract steps 3 and 4. Carrier-only: a broker never sends a
  // contract for signature, they are sent one. Their half of the flow is
  // /sign, which is public and unlisted here because it has no session at
  // all — an unlisted path still needs a signed-in user, and a broker
  // signing from an email has no account to sign in with.
  // No longer a sidebar entry — reached from Contracts and from a contract's
  // own record — so this rule is what guards it now that nothing hides the
  // link from the wrong role.
  // …and the carrier ADMIN's alone, for the same reason: this is the screen
  // that names who signs and sends the envelope.
  { pattern: "/contracts/signatures", requires: "carrier_admin",
    only: ["carrier_admin"], carrierAdminOnly: true },
  { pattern: "/brokers/:brokerId", requires: "carrier_admin", only: ["carrier_admin"] },
  // What a broker has sent this carrier, from the Home card. Same gate as the
  // broker record it sits beside; both carrier seats rank as carrier_admin.
  { pattern: "/brokers/:brokerId/files", requires: "carrier_admin",
    only: ["carrier_admin"] },
  { pattern: "/outputs/templates/:id", requires: "carrier_admin" },
  // Exception triage. Open to BROKER seats too, not only the carrier: a broker
  // who ran a bordereau has to be able to see what failed on their own file and
  // correct it. The screen is scoped by the export id in the path, and the
  // server decides what that id may be — output_exports carries the broker the
  // run was made for, so a broker reaches its OWN runs and 404s on anyone
  // else's (carrier_scope.assert_can_read_export). Listing the roles here only
  // stops the UI bouncing them before the server ever gets asked.
  { pattern: "/uploads/:uploadId/exceptions", requires: "operator",
    only: ["carrier_admin", "kavachio_admin", "broker_admin", "operator"] },
  { pattern: "/uploads/:uploadId/exceptions/rule/:ruleId", requires: "operator",
    only: ["carrier_admin", "kavachio_admin", "broker_admin", "operator"] },
  { pattern: "/runs", requires: "carrier_admin" },
  { pattern: "/calendar", requires: "carrier_admin" },
  { pattern: "/bordereau-calendar", requires: "carrier_admin" },
  // How files reach this carrier, and everything that has landed. Carrier-only:
  // the routes name the carrier's brokers and their folder addresses.
  { pattern: "/files", requires: "carrier_admin" },
  // Kept so the redirects to /files are not bounced before they can run.
  { pattern: "/intake", requires: "carrier_admin" },
  { pattern: "/intake/arrivals", requires: "carrier_admin" },
  { pattern: "/tenants", requires: "kavachio_admin" },
  { pattern: "/tenants/new", requires: "kavachio_admin" },
  { pattern: "/tenants/:mga", requires: "kavachio_admin" },

  // --- Tenant admin (org / carrier / setup / user administration) --------
  { pattern: "/welcome", requires: "carrier_admin" },
  // Program Management — carrier-scoped oversight of the program book.
  { pattern: "/program-management", requires: "carrier_admin" },
  { pattern: "/tenant", requires: "carrier_admin" },
  { pattern: "/users", requires: "carrier_admin" },
  // Invite a broker (the page's only form since carrier users were retired).
  { pattern: "/users/new", requires: "carrier_admin" },
  { pattern: "/direct/setup", requires: "carrier_admin" },
  { pattern: "/direct/setups", requires: "carrier_admin" },
  { pattern: "/direct/setups/:id", requires: "carrier_admin" },
  { pattern: "/direct/setups/:id/edit", requires: "carrier_admin" },
  // Rule library — tenant_admin sees their own tenant's rules, kavachio_admin
  // the platform-wide ones. The backend scopes the rows by role.
  // READ by both carrier seats, WRITTEN by the carrier admin alone. A rule in
  // the library is applied to every bordereau this carrier validates, so
  // adding or changing one belongs to the one person accountable for the book;
  // a carrier user runs the setups that put these rules to work, so they see
  // the list, read-only.
  //
  // `carrierAdminOnly` rather than `only` on the two write screens, because
  // the two carrier seats hold the same DB role and nothing in the token tells
  // them apart — the organisation's owner pointer does (see
  // hooks/useCarrierSeat). The server enforces the same split on its four
  // write endpoints (carrier_scope.require_carrier_admin).
  { pattern: "/rule-library", requires: "carrier_admin" },
  { pattern: "/rule-library/new", requires: "carrier_admin", carrierAdminOnly: true },
  { pattern: "/rule-library/:id/edit", requires: "carrier_admin",
    carrierAdminOnly: true },
];

/** The rule covering a path, or null when any signed-in user may open it. */
function ruleFor(pathname: string) {
  return ROUTE_ACCESS.find(r => matchPath({ path: r.pattern, end: true }, pathname)) ?? null;
}

/** The minimum role a path needs, or null when any signed-in user may open it. */
export function requiredRoleFor(pathname: string): Role | null {
  return ruleFor(pathname)?.requires ?? null;
}

/** True when `role` satisfies `required` (superset chain). */
export function hasRole(required: Role, role: Role | null = userRole()): boolean {
  return role !== null && RANK[role] >= RANK[required];
}

/** True when the current user may open `pathname`.
 *
 *  `seat` narrows the carrier side (see `carrierAdminOnly`). It is read from
 *  the persisted auth slice, so a returning session knows it on the first
 *  paint. A fresh login does not — the organisation has not been fetched yet —
 *  and that case deliberately FAILS OPEN: bouncing the carrier admin off their
 *  own signature screen for the half-second before the fetch lands is worse
 *  than briefly showing a carrier user a screen whose every request the server
 *  refuses. This was never the security boundary; the API is.
 */
export function canAccessPath(
  pathname: string,
  role: Role | null = userRole(),
  seat: CarrierSeat = seatOf(getUser(), getTenantBrand()),
): boolean {
  if (role === null) return false;               // signed out — RequireAuth handles it
  const rule = ruleFor(pathname);
  if (rule === null) return true;                // unlisted → any signed-in user
  if (rule.carrierAdminOnly && role === "carrier_admin" && seat === "user") return false;
  if (rule.only) return rule.only.includes(role);
  return hasRole(rule.requires, role);
}

/**
 * The role's own landing screen. Used both for "/" and as the destination when
 * a user is bounced off a screen they may not open — so nobody ever lands on a
 * second forbidden page (a platform admin has no tenant Home, and vice versa).
 */
export function landingPath(role: Role | null = userRole()): string {
  if (role === "kavachio_admin") return "/admin/dashboard";
  // A broker seat has no carrier Home — every card on it reads carrier data
  // they are refused, so they would land on a page of blanks.
  if (role === "broker_admin") return "/broker";
  // Broker users were retired on 29 Sep 2026 and the server refuses their
  // sign-in; a session left over from before lands somewhere that exists.
  if (role === "operator") return "/broker/bordereau";
  return "/home";
}
