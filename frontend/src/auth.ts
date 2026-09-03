// ---------------------------------------------------------------------------
// Auth helper API.
//
// State lives in the Redux store's auth slice and is persisted to localStorage
// by redux-persist (see src/store). These helpers delegate to the store so
// non-React code (axios interceptors, timers) and components share one source
// of truth without every call site needing hooks. Prefer useSelector in new
// React code; these functions remain for the existing call sites.
// ---------------------------------------------------------------------------

import { store } from "./store";
import {
  accessTokenSet, authCleared, authSet, tenantBrandSet, userSet,
  type TenantBrand, type User,
} from "./store/authSlice";

export type { TenantBrand, User };

// ---------------------------------------------------------------------------
// Role model (prototype v1)
// The app has three roles:
//   tenant_user    — "Operator": runs files, reviews exceptions
//   tenant_admin   — manages setups, parties, users, org settings
//   kavachio_admin — platform admin: tenants + data-mapping queue (superset)
//
// The backend still emits the legacy set (admin | ops | read_only). We normalize
// on read so the UI can move to the new model before the backend does. Once the
// backend returns the new values, this mapping is a harmless pass-through.
// TODO(backend): emit tenant_user / tenant_admin / kavachio_admin directly.
// ---------------------------------------------------------------------------
// FOUR seats. kavachio_admin and carrier_admin are the CARRIER side; the two
// broker seats belong to a BROKER organisation and sit on their own axis — a
// broker admin is not "above" or "below" a carrier admin, it is elsewhere.
export type Role =
  | "kavachio_admin" | "carrier_admin" | "broker_admin" | "operator";

const ROLE_ALIASES: Record<string, Role> = {
  // legacy MGA-era spellings — a tenant IS a carrier
  admin: "carrier_admin",
  ops: "carrier_admin",
  read_only: "carrier_admin",
  // current
  kavachio_admin: "kavachio_admin",
  carrier_admin: "carrier_admin",
  broker_admin: "broker_admin",
  operator: "operator",
  // legacy spellings from the MGA era — a tenant IS a carrier
  tenant_admin: "carrier_admin",
  tenant_user: "carrier_admin",
  broker_operator: "operator",
};

export function normalizeRole(raw?: string | null): Role {
  // Unknown falls back to the least-privileged seat, never to an admin.
  return ROLE_ALIASES[(raw ?? "").trim()] ?? "operator";
}

export function getUser(): User | null {
  return store.getState().auth.user;
}

// Broadcast so same-tab listeners (e.g. the sidebar user block) re-read the
// stored user after it changes. Kept alongside the store for the existing
// listeners; new code can subscribe to the store instead.
export const AUTH_EVENT = "kavachio:auth";
function emitAuthEvent() {
  try { window.dispatchEvent(new Event(AUTH_EVENT)); } catch { /* no-window */ }
}

export function setUser(u: User) {
  store.dispatch(userSet(u));
  emitAuthEvent();
}
export function clearUser() {
  store.dispatch(authCleared());
  emitAuthEvent();
}

// --- JWT token storage --------------------------------------------------
// Tokens sit in the persisted auth slice (localStorage via redux-persist —
// accepted XSS tradeoff, unchanged from phase 1). The access token is attached
// to every request; the refresh token (7-day) mints new access tokens until it
// expires, at which point the client is logged out.
export function getAccessToken(): string | null {
  return store.getState().auth.accessToken;
}
export function getRefreshToken(): string | null {
  return store.getState().auth.refreshToken;
}
export function setAccessToken(t: string) {
  store.dispatch(accessTokenSet(t));
}

/** The shape POST /auth/login (and /auth/refresh) returns. */
export type AuthResponse = User & {
  access_token?: string;
  refresh_token?: string;
};

/** Persist the full login response: user profile + both tokens. */
export function setAuth(data: AuthResponse) {
  store.dispatch(authSet({
    user: {
      id: data.id, email: data.email, full_name: data.full_name,
      role: data.role, mga: data.mga, tenant_id: data.tenant_id,
    },
    accessToken: data.access_token,
    refreshToken: data.refresh_token,
  }));
  emitAuthEvent();
}

/** Clear everything — user profile and both tokens. Use on logout / 401. */
export function clearAuth() {
  store.dispatch(authCleared());
  emitAuthEvent();
}

export function currentMga(): string {
  const s = store.getState().auth;
  return s.user?.mga ?? s.mga ?? "default";
}

// --- tenant branding cache --------------------------------------------------
// The sidebar shows the organization's logo + name next to the Kavachio brand.
// Cached in the persisted auth slice so the sidebar paints instantly and can
// update live — reusing AUTH_EVENT — the moment the logo changes, no reload.
export function getTenantBrand(): TenantBrand | null {
  return store.getState().auth.tenantBrand;
}
export function setTenantBrand(t: TenantBrand) {
  store.dispatch(tenantBrandSet(t));
  emitAuthEvent();
}

// --- role helpers -----------------------------------------------------------
export function userRole(): Role | null {
  const u = getUser();
  return u ? normalizeRole(u.role) : null;
}
/** Platform-level admin (Tenants, Data Mapping Queue). */
export function isKavachioAdmin(): boolean {
  return userRole() === "kavachio_admin";
}
/** A BROKER seat (broker_admin or operator).
 *
 * These users belong to a broker organisation, not to a carrier, so their token
 * carries no tenant at all — currentMga() falls back to the literal "default"
 * for them. Any carrier-tenant call they make is answered "no tenant bound to
 * this user", which is correct on the server and useless in the UI. Anything
 * that reads tenant-scoped data has to check this BEFORE fetching, not handle
 * the 403 afterwards.
 */
export function isBrokerSeat(): boolean {
  const r = userRole();
  return r === "broker_admin" || r === "operator";
}
/** Can perform tenant-admin actions. kavachio_admin is a superset. */
export function isTenantAdmin(): boolean {
  const r = userRole();
  return r === "carrier_admin" || r === "kavachio_admin";
}
// These labels were written when a tenant WAS a broker. In the carrier-centric
// model a tenant is a CARRIER, so "tenant_admin = Broker Admin" named the wrong
// organisation on every screen it appeared on.
export const ROLE_LABEL: Record<Role, string> = {
  kavachio_admin: "Kavachio Admin",
  carrier_admin:  "Carrier Admin",
  broker_admin:   "Broker Admin",
  operator:       "Operator",
};

// --- refresh-token expiry → automatic logout ---------------------------------
// The API interceptor only logs out when a request 401s AND the refresh fails,
// so an idle tab (or a page load with a long-dead token) still looks signed in.
// These helpers decode the refresh JWT's `exp` so the app can log out the
// moment the session truly ends, without waiting for a failed request.

function tokenExpMs(token: string | null): number | null {
  if (!token) return null;
  try {
    const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    const payload = JSON.parse(atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, "=")));
    return typeof payload.exp === "number" ? payload.exp * 1000 : null;
  } catch {
    return null;
  }
}

/** True when a refresh token exists but its `exp` has passed. */
export function isRefreshTokenExpired(): boolean {
  const exp = tokenExpMs(getRefreshToken());
  return exp !== null && Date.now() >= exp;
}

/** True when the access token is missing, expired, or about to expire.
 *  The 30s skew means a request never leaves with a token that dies mid-flight.
 *
 *  An UNREADABLE token (exp missing / not a JWT) is deliberately NOT treated as
 *  stale. It used to be, which meant `tokenExpMs` returning null pinned this to
 *  `true` forever — and since the request interceptor awaits a refresh before
 *  the real call, EVERY request became two sequential round-trips for the rest
 *  of the session. Sending it and letting a 401 drive the refresh costs at most
 *  one wasted call, once, instead of doubling every call. A genuinely absent
 *  token still refreshes up front, since there's nothing to send. */
export function isAccessTokenStale(): boolean {
  const tok = getAccessToken();
  if (!tok) return true;                 // nothing to send — refresh first
  const exp = tokenExpMs(tok);
  if (exp === null) return false;        // can't read exp — send it; 401 refreshes
  return Date.now() >= exp - 30_000;
}

function forceLogout() {
  clearAuth();
  if (window.location.pathname !== "/login") window.location.assign("/login");
}

let logoutTimer: number | undefined;

/**
 * (Re)arm a timer that logs the user out exactly when the refresh token
 * expires. Call once on app start and again whenever auth changes (login
 * stores a new token). Safe to call repeatedly — the previous timer is
 * replaced. Re-arms itself at fire time, which both handles setTimeout's
 * ~24.8-day cap and re-checks real time after laptop sleep / clock changes.
 */
export function armAutoLogout() {
  if (logoutTimer !== undefined) {
    window.clearTimeout(logoutTimer);
    logoutTimer = undefined;
  }
  const exp = tokenExpMs(getRefreshToken());
  if (exp === null) return; // signed out — nothing to schedule
  const remaining = exp - Date.now();
  if (remaining <= 0) {
    forceLogout();
    return;
  }
  logoutTimer = window.setTimeout(armAutoLogout, Math.min(remaining, 2 ** 31 - 1));
}
