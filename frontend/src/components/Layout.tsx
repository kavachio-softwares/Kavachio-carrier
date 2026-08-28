import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, Building2, LogOut, UserCog, Zap,Database, Users2, Boxes, ChevronRight, ChevronLeft, Layers, ListChecks, ClipboardList,
} from "lucide-react";
import { AUTH_EVENT, clearAuth, currentMga, getRefreshToken, getTenantBrand, getUser, isKavachioAdmin, normalizeRole, ROLE_LABEL, setTenantBrand, type Role } from "../auth";
import { canAccessPath, hasRole } from "../access";
import { api, getDeduped } from "../api/client";
import { GlobalLoadingOverlay } from "./Busy";
import PlatformNotificationCard from "./PlatformNotificationCard";
import DeadlineReminderCard from "./DeadlineReminderCard";
import NotificationBell from "./NotificationBell";
import { initials } from "../branding";

// Items carry no role of their own: whether one is visible is derived from the
// destination's entry in ROUTE_ACCESS (access.ts), which is also what guards the
// route. One source of truth, so the sidebar can never offer a link that would
// bounce the user straight back to their dashboard.
type Item = { to: string; label: string; icon: React.ElementType };

// Nav grouped to match the prototype's sidebar. All existing destinations are
// preserved; each item is shown only when the current role may actually open it
// (ROUTE_ACCESS in access.ts).
// `requires` on a GROUP gates only the section HEADER (matching the prototype's
// data-roles on the group label); items still render when the header is hidden —
// so an Operator sees Dashboard / Process / Trading Partners as a flat,
// header-less list.
const GROUPS: { title: string; requires?: Role; items: Item[] }[] = [
  {
    title: "Run",
    requires: "tenant_admin",
    items: [
      { to: "/home", label: "Dashboard", icon: LayoutDashboard },
      // Hidden from the sidebar for now. The /program-management route still
      // exists and is still reached from a carrier's page — only the nav entry
      // is commented out.
      // { to: "/program-management", label: "Program Management", icon: ClipboardList },
      { to: "/direct", label: "Process Bordereau", icon: Zap },
      // My Calendar is deliberately NOT listed. Deadlines belong to a specific
      // carrier + program, so the calendar now lives inside that setup's own
      // screen where both are already fixed. The /calendar route still exists
      // and is still reached from the bell, the deadline card, the dashboard
      // link and the deadline email — it is the only place a program with no
      // bordereau setup yet can have its schedule set.
    ],
  },
  {
    title: "Configure",
    requires: "tenant_admin",
    items: [
      { to: "/parties", label: "All Carriers", icon: Users2 },
      { to: "/direct/setups", label: "Bordereau Setups", icon: Layers },
    ],
  },
  {
    title: "Admin",
    requires: "tenant_admin",
    items: [
      { to: "/tenant", label: "Organization", icon: Building2 },
      { to: "/users", label: "Users & Roles", icon: UserCog },
      { to: "/rule-library", label: "Rule Library", icon: ListChecks },
    ],
  },
  {
    title: "Platform",
    requires: "kavachio_admin",
    items: [
      { to: "/admin/mapping-tasks", label: "Data Mapping Queue", icon: Database },
      { to: "/tenants", label: "Brokers", icon: Boxes },
    ],
  },
];

// Kavachio platform admins get a dedicated, minimal sidebar — none of the
// tenant-level Run/Configure/Admin items. Flat list (empty title → no header).
const ADMIN_GROUPS: typeof GROUPS = [
  {
    title: "",
    items: [
      { to: "/admin/dashboard", label: "Dashboard", icon: LayoutDashboard },
      { to: "/admin/mapping-tasks", label: "Data Mapping Queue", icon: Database },
      { to: "/tenants", label: "Brokers", icon: Boxes },
      { to: "/rule-library", label: "Rule Library", icon: ListChecks },
    ],
  },
];

// Several screens have no sidebar entry of their own — they're reached only as
// a sub-flow of a nav item (or, for Run History/exception triage, of whichever
// nav item actually linked into them, carried via `?from=`). Returns the `to`
// of the nav item that should stay highlighted for a given location, or null.
function subScreenOwner(pathname: string, search: string): string | null {
  const under = (base: string) => pathname === base || pathname.startsWith(base + "/");

  // The column-mapping workflow (/uploads/mapper/*) belongs to Data Mapping Queue.
  if (pathname.startsWith("/uploads/mapper")) return "/admin/mapping-tasks";

  // Programs / Outputs are the orphaned party → program → template → output
  // stepper — no sidebar entry of their own, so they roll up under Trading
  // Partners (programs/contracts are scoped to a carrier).
  if (under("/programs") || under("/outputs")) return "/parties";

  if (under("/parties")) return "/parties";
  if (under("/tenants")) return "/tenants";
  if (under("/users")) return "/users";
  if (under("/rule-library")) return "/rule-library";

  // A single setup's read-only view has no sidebar entry of its own — and
  // neither does the Setup screen itself (reached from Process Bordereau, the
  // All Setups empty state and onboarding). Both roll up under "All Setups".
  if (pathname === "/direct/setup" || pathname.startsWith("/direct/setup/")) return "/direct/setups";
  if (pathname.startsWith("/direct/setups/")) return "/direct/setups";

  // Run History + exception triage are linked from both Dashboard and Process
  // Bordereau — whichever one the user actually came from stays highlighted.
  const isRunsOrExceptions = pathname === "/runs"
    || /^\/uploads\/[^/]+\/exceptions(\/rule\/[^/]+)?$/.test(pathname);
  if (isRunsOrExceptions) {
    const from = new URLSearchParams(search).get("from");
    if (from === "home") return "/home";
    if (from === "direct") return "/direct";
  }
  return null;
}

export default function Layout() {
  const nav = useNavigate();
  const loc = useLocation();
  // Re-read the stored user when it changes (e.g. after a profile name edit)
  // so the sidebar block reflects the new name without a full reload.
  const [, bump] = useState(0);
  useEffect(() => {
    const onAuth = () => bump(n => n + 1);
    window.addEventListener(AUTH_EVENT, onAuth);
    return () => window.removeEventListener(AUTH_EVENT, onAuth);
  }, []);
  const user = getUser();
  const mga = currentMga();
  // Pull the tenant's branding (logo + name) once per org so the sidebar can
  // co-brand under the Kavachio mark. setTenantBrand caches it and fires
  // AUTH_EVENT, so the block above re-renders with the fresh logo.
  useEffect(() => {
    if (!mga || mga === "default" || isKavachioAdmin()) return;
    let cancelled = false;
    getDeduped<{ legal_name?: string | null; logo?: string | null }>(`/tenants/${mga}`)
      .then(r => { if (!cancelled) setTenantBrand({ mga, legal_name: r.data?.legal_name, logo: r.data?.logo ?? null }); })
      .catch(() => { /* sidebar just falls back to the Kavachio mark */ });
    return () => { cancelled = true; };
  }, [mga]);
  const brand = getTenantBrand();
  // Icon-only rail toggle. Defaults to EXPANDED on desktop widths so large
  // screens always open fully shown; only narrow screens (< 1024px) start as the
  // icon rail. The choice survives in-app navigation (Layout isn't remounted) and
  // resets to this width-based default on a full reload. Purely presentational —
  // all routes/actions are unchanged in either state.
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try { return window.innerWidth < 1024; } catch { return false; }
  });
  const toggleSidebar = () => setCollapsed(c => !c);
  // Exposes the sidebar's current width as a CSS var so a `position: fixed`
  // element elsewhere (e.g. a page's bottom action bar) can align its `left`
  // with the actual content area instead of guessing — kept in sync with the
  // same collapsed/expanded widths as .proto-side/.proto-side.collapsed.
  useEffect(() => {
    document.documentElement.style.setProperty("--sidebar-w", collapsed ? "74px" : "256px");
  }, [collapsed]);
  // Gate each nav item by whether the current role may actually open its route
  // (ROUTE_ACCESS — the same map RequireAccess enforces), and each group HEADER
  // by the group's own `requires`. Roles are a superset chain, so tenant_admin+
  // passes tenant_admin gates, etc.
  const canSee = (requires?: Role) => requires === undefined || hasRole(requires);
  const groups = isKavachioAdmin()
    ? ADMIN_GROUPS
    : GROUPS
        .map(g => ({ ...g, items: g.items.filter(i => canAccessPath(i.to)) }))
        .filter(g => g.items.length > 0);
  const roleLabel = user ? ROLE_LABEL[normalizeRole(user.role)] : "";
  // The workspace card's contents — shared by its interactive (admin) and
  // static (Operator) forms below.
  const workspaceIdentity = brand?.legal_name ? (
    <>
      <span className="wscard-ava">
        {brand.logo
          ? <img className="wscard-logo" src={brand.logo} alt={brand.legal_name} />
          : <span className="wscard-logo wscard-fallback">{initials(brand.legal_name)}</span>}
        <span className="wscard-dot" aria-hidden="true" />
      </span>
      <span className="wscard-meta">
        <span className="wscard-name">{brand.legal_name}</span>
        <span className="wscard-sub">Organization</span>
      </span>
    </>
  ) : null;

  return (
    <div className="flex min-h-screen">
      <GlobalLoadingOverlay />
      {/* Sign-in message for Kavachio platform admins (self-gating: renders
          nothing for other roles, or when nothing is waiting). Mounted in the
          shell so it survives navigation between admin screens. */}
      <PlatformNotificationCard />
      {/* C-9: the broker's own overdue / due-soon reminders. Mounted here so they
          reach the user on every screen, not only on My Calendar. Self-gating —
          renders nothing when there is nothing unread, or for platform admins. */}
      <DeadlineReminderCard />
      <aside className={`proto-side${collapsed ? " collapsed" : ""}`}>
        <button
          type="button"
          className="side-toggle"
          onClick={toggleSidebar}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
        >
          {collapsed ? <ChevronRight size={14} /> : <ChevronLeft size={14} />}
        </button>
        {/* Brand head: the Kavachio product mark, and beneath it the tenant's
            organization presented as an interactive "workspace" card — a logo,
            the org name and a live dot — so it reads as the space you're working
            in (and opens Organization settings), not a second brand block. */}
        <div className="brandhead">
          <div className="brand">
            <img className="shield" src="/kavachio_sidebar_logo.png" alt="Kavachio" />
            <div>
              <h1>Kavachio</h1>
              <div className="tag">Bordereau Platform</div>
            </div>
          </div>

          {/* Organization settings are admin-only, so for an Operator the card
              is identity only — same logo, name and live dot, but no chevron
              and nothing to click (it would only bounce them to the dashboard).
              Admins get the interactive card exactly as before. */}
          {!isKavachioAdmin() && brand?.legal_name && (
            canAccessPath("/tenant") ? (
              <button type="button" className="wscard" title="Open Organization settings"
                onClick={() => nav("/tenant")}>
                {workspaceIdentity}
                <ChevronRight className="wscard-go" size={15} strokeWidth={2} />
              </button>
            ) : (
              <div className="wscard wscard-static">{workspaceIdentity}</div>
            )
          )}
        </div>

        <nav className="nav">
          {groups.map(group => (
            <div key={group.title}>
              {group.title && canSee(group.requires) && <div className="group">{group.title}</div>}
              {group.items.map(({ to, label, icon: Icon }) => {
                const subScreenActive = subScreenOwner(loc.pathname, loc.search) === to;
                return (
                  <NavLink
                    key={to} to={to} end title={label}
                    className={({ isActive }) => `item${isActive || subScreenActive ? " active" : ""}`}
                  >
                    <Icon className="ic" strokeWidth={1.8} />
                    <span className="item-label">{label}</span>
                  </NavLink>
                );
              })}
            </div>
          ))}
        </nav>

        <div className="side-foot">
          {/* Notifications live here, beside the account, because the deadline
              reminder card that greets you on sign-in is DISMISSABLE — and
              dismissing it only marks the reminders seen. Without a permanent
              way back there was none, on any screen. The bell lists every
              reminder whether or not it is still unread; the badge counts only
              what you haven't looked at. Hidden for platform admins, who have no
              submission deadlines of their own — the same rule the card uses. */}
          <div style={{
            display: "flex", alignItems: "center", gap: 6,
            flexDirection: collapsed ? "column" : "row",
          }}>
          <div
            className="side-user side-user-link"
            style={collapsed ? undefined : { flex: 1, minWidth: 0 }}
            role="button"
            tabIndex={0}
            title="View your account"
            onClick={() => nav("/profile")}
            onKeyDown={e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); nav("/profile"); } }}
          >
            <div className="av">{initials(user?.full_name)}</div>
            <div style={{ minWidth: 0 }}>
              <div className="nm" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {user?.full_name ?? "—"}
              </div>
              {/* Just the person here — the organization is shown up top in the
                  workspace card, so we don't repeat the org code in the footer. */}
              <div className="rl">{roleLabel}</div>
            </div>
          </div>
            {!isKavachioAdmin() && <NotificationBell />}
          </div>
          <button className="signout" title="Sign out" onClick={() => {
            // Send the refresh token in the body, read SYNCHRONOUSLY here. The
            // axios auth-header interceptor runs in a microtask — i.e. AFTER the
            // synchronous clearAuth() below — so the header would race and go out
            // empty. The body is captured now, before clearAuth wipes it, so the
            // logout audit row gets full user context (user_id/tenant_id/actor).
            api.post("/auth/logout", { refresh_token: getRefreshToken() }).catch(() => {});
            clearAuth(); nav("/login");
          }}>
            <LogOut size={12} /> <span className="item-label">Sign out</span>
          </button>
          <div className="side-ver">v0.9 · POC build</div>
        </div>
      </aside>

      <main className="flex-1 min-w-0 bg-bg">
        <Outlet />
      </main>
    </div>
  );
}

export function PageHeader({ title, subtitle, action }:
  { title: string | null; subtitle?: string; action?: React.ReactNode }) {
  return (
    <div className="px-8 pt-7 pb-4 border-b border-border bg-white">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-semibold tracking-tight">{title}</h1>
          {subtitle && <p className="text-sm text-ink-muted mt-1">{subtitle}</p>}
        </div>
        {action}
      </div>
    </div>
  );
}
export function PageBody({ children }: { children: React.ReactNode }) {
  return <div className="px-8 py-6 space-y-5">{children}</div>;
}
