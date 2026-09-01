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
export const ROUTE_ACCESS: { pattern: string; requires: Role }[] = [
  // --- Platform admin (Kavachio staff, cross-tenant) ---------------------
  { pattern: "/admin/dashboard", requires: "kavachio_admin" },
  { pattern: "/admin/mapping-tasks", requires: "kavachio_admin" },
  // The column-mapping workflow is reached only from the queue above.
  { pattern: "/uploads/mapper/:mapperId", requires: "kavachio_admin" },
  { pattern: "/admin/users", requires: "kavachio_admin" },
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
  { pattern: "/approvals", requires: "carrier_admin" },
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

/** The minimum role a path needs, or null when any signed-in user may open it. */
export function requiredRoleFor(pathname: string): Role | null {
  const hit = ROUTE_ACCESS.find(r => matchPath({ path: r.pattern, end: true }, pathname));
  return hit ? hit.requires : null;
}

/** True when `role` satisfies `required` (superset chain). */
export function hasRole(required: Role, role: Role | null = userRole()): boolean {
  return role !== null && RANK[role] >= RANK[required];
}

/** True when the current user may open `pathname`. */
export function canAccessPath(pathname: string, role: Role | null = userRole()): boolean {
  if (role === null) return false;               // signed out — RequireAuth handles it
  const required = requiredRoleFor(pathname);
  return required === null || hasRole(required, role);
}

/**
 * The role's own landing screen. Used both for "/" and as the destination when
 * a user is bounced off a screen they may not open — so nobody ever lands on a
 * second forbidden page (a platform admin has no tenant Home, and vice versa).
 */
export function landingPath(role: Role | null = userRole()): string {
  return role === "kavachio_admin" ? "/admin/dashboard" : "/home";
}
