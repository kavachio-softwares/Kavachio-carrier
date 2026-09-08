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
import { userRole, type Role } from "./auth";

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
export const ROUTE_ACCESS: { pattern: string; requires: Role; only?: Role[] }[] = [
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
  // An operator is a seat inside the broker, not a manager of it: their own
  // landing screen, and no access to the admin views above.
  { pattern: "/operator", requires: "operator", only: ["operator"] },
  { pattern: "/broker/contracts", requires: "broker_admin", only: ["broker_admin"] },
  { pattern: "/broker/contracts/new", requires: "broker_admin", only: ["broker_admin"] },
  // Both broker seats, unlike the rest of this block. Running the bordereau IS
  // the operator's job — the seat exists for it — and an admin does it too, so
  // this is the one broker screen that is not the admin's alone.
  { pattern: "/broker/bordereau", requires: "operator", only: ["broker_admin", "operator"] },
  // The broker staffs itself here. An operator is a seat inside that team, not
  // a manager of it, so this one is the admin's alone — the database says the
  // same thing (only a broker admin may create an operator).
  { pattern: "/broker/users", requires: "broker_admin", only: ["broker_admin"] },

  // --- Carrier screens -----------------------------------------------------
  // These were unlisted, and an unlisted path falls through to "any signed-in
  // user". Harmless while everyone signing in was a carrier user; the moment a
  // broker could sign in they got the whole carrier app by default. Listing
  // them explicitly is what actually closes that.
  { pattern: "/direct", requires: "carrier_admin" },
  { pattern: "/parties", requires: "carrier_admin" },
  { pattern: "/parties/new", requires: "carrier_admin" },
  { pattern: "/parties/:id", requires: "carrier_admin" },
  { pattern: "/programs", requires: "carrier_admin" },
  { pattern: "/programs/new", requires: "carrier_admin" },
  { pattern: "/programs/:programId/brokers", requires: "carrier_admin" },
  { pattern: "/programs/:programId/contracts/:contractId", requires: "carrier_admin" },
  // The contract RECORD screens. The carrier-wide list and the create form are
  // the carrier's — a broker reaches its own contracts through My Contracts.
  { pattern: "/contracts", requires: "carrier_admin" },
  { pattern: "/contracts/new", requires: "carrier_admin" },
  { pattern: "/contracts/upload", requires: "carrier_admin" },
  // The record itself is open to both sides, because both have business with
  // it: the carrier decides on it, and the broker has to attach the documents
  // it defers to and correct it after a rejection. The API scopes what each
  // one can see and do — a broker gets a 404 on anyone else's contract.
  { pattern: "/contracts/:contractId", requires: "operator",
    only: ["broker_admin", "operator", "carrier_admin", "kavachio_admin"] },
  // Signing is between the two organisations, so both sides reach it. The
  // screen writes nothing, but it names the people who would sign.
  { pattern: "/contracts/:contractId/signature", requires: "operator",
    only: ["broker_admin", "operator", "carrier_admin", "kavachio_admin"] },
  // The gate. Only the carrier decides.
  { pattern: "/approvals", requires: "carrier_admin" },
  { pattern: "/brokers", requires: "carrier_admin" },
  // Create-a-Contract steps 3 and 4. Carrier-only: a broker never sends a
  // contract for signature, they are sent one. Their half of the flow is
  // /sign, which is public and unlisted here because it has no session at
  // all — an unlisted path still needs a signed-in user, and a broker
  // signing from an email has no account to sign in with.
  // No longer a sidebar entry — reached from Contracts and from a contract's
  // own record — so this rule is what guards it now that nothing hides the
  // link from the wrong role.
  { pattern: "/contracts/signatures", requires: "carrier_admin" },
  { pattern: "/brokers/:brokerId", requires: "carrier_admin" },
  { pattern: "/outputs", requires: "carrier_admin" },
  { pattern: "/outputs/new-template", requires: "carrier_admin" },
  { pattern: "/outputs/generate", requires: "carrier_admin" },
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
  // The one approval in the platform. Reading it is harmless, but only a
  // carrier admin can decide — the API enforces that independently.
  { pattern: "/users/new", requires: "carrier_admin" },
  { pattern: "/direct/setup", requires: "carrier_admin" },
  { pattern: "/direct/setups", requires: "carrier_admin" },
  { pattern: "/direct/setups/:id", requires: "carrier_admin" },
  { pattern: "/direct/setups/:id/edit", requires: "carrier_admin" },
  // Rule library — tenant_admin sees their own tenant's rules, kavachio_admin
  // the platform-wide ones. The backend scopes the rows by role.
  { pattern: "/rule-library", requires: "carrier_admin" },
  { pattern: "/rule-library/new", requires: "carrier_admin" },
  { pattern: "/rule-library/:id/edit", requires: "carrier_admin" },
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

/** True when the current user may open `pathname`. */
export function canAccessPath(pathname: string, role: Role | null = userRole()): boolean {
  if (role === null) return false;               // signed out — RequireAuth handles it
  const rule = ruleFor(pathname);
  if (rule === null) return true;                // unlisted → any signed-in user
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
  if (role === "operator") return "/operator";
  return "/home";
}
