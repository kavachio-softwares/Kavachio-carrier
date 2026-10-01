import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { isKavachioAdmin, normalizeRole } from "../auth";
import { fmtDateTime, localDayStart, localDayEnd } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

type TenantRow = {
  mga: string; name: string; code: string;
  tenant_type?: string | null; users: number; setups: number; is_active: boolean;
  legal_name?: string | null; currency?: string | null;
  created_at?: string | null; modified_at?: string | null;
};
type U = { id: number; email: string; full_name: string; role: string; status: string; last_login_at?: string | null;
  /** The organisation's owner — its Carrier Admin. Every other carrier_admin row is a Carrier User. */
  is_owner?: boolean };
type Program = {
  id: number; name: string; product_line?: string | null; status?: string | null;
  /** How many contracts hang off it. Sent only on the PAGED answer — a
   *  screen holding ten programmes cannot count them itself. */
  contract_count?: number;
};
type Contract = {
  id: number; name?: string | null; filename?: string | null; status?: string | null;
  created_at?: string | null; clause_count?: number;
  program_id?: number; program_name?: string;
  broker_party_id?: number | null; broker_name?: string | null;
  inception_dt?: string | null; expiry_dt?: string | null;
};
/** A contract's stage in words and colour — the stored value is a code. */
function contractBadge(s?: string | null) {
  const v = (s ?? "").toLowerCase();
  const map: Record<string, [string, string]> = {
    draft: ["b-mut", "Draft"], drafted: ["b-mut", "Draft"],
    pending: ["b-warn", "Awaiting review"], in_review: ["b-warn", "Out for review"],
    agreed: ["b-warn", "Agreed"], signed: ["b-ok", "Signed"],
    in_force: ["b-ok", "In force"], active: ["b-ok", "Active"], approved: ["b-ok", "Approved"],
    rejected: ["b-crit", "Rejected"], declined: ["b-crit", "Declined"],
    voided: ["b-mut", "Voided"], expired: ["b-mut", "Expired"],
  };
  const [cls, label] = map[v] ?? ["b-mut", v ? v.replace(/_/g, " ").replace(/^./, c => c.toUpperCase()) : "—"];
  return { cls, label };
}
/** "1 Jan 2026" from an ISO date or timestamp. */
function fmtDay(iso?: string | null) {
  if (!iso) return null;
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}
// A broker from the carrier's directory (GET /brokers?mga=) — read only for
// the programme → broker names shown in the Brokers column.
type DirBroker = { id: number; legal_name: string; programmes?: { id: number; name: string }[] };
// One broker on one programme (GET /programs/{id}/brokers?mga=) — `status` is
// its link to that programme.
type ProgBroker = { id: number; legal_name: string; status?: string | null; contract_count?: number };
function linkBadge(s?: string | null) {
  const v = (s ?? "").toLowerCase();
  if (v === "active") return { cls: "b-ok", label: "Active" };
  if (v === "pending_approval") return { cls: "b-warn", label: "Awaiting approval" };
  if (v === "ended") return { cls: "b-mut", label: "Ended" };
  return { cls: "b-mut", label: s || "—" };
}
const DRAWER_PAGE = 5;
type Run = {
  landing_id: number; source_filename?: string | null; program_name?: string | null;
  created_at?: string | null; exception_count?: number; status?: string | null;
};

const TYPE_LABEL: Record<string, string> = {
  carrier: "Carrier", mga: "MGA", mgu: "MGU", broker: "Broker", tpa: "TPA", reinsurer: "Reinsurer",
};
// The org Details tab only offers the types a tenant can be provisioned as
// (mirrors the Add Broker form + backend allow-list). A legacy carrier/reinsurer
// value is still shown as a read-only option so its label doesn't vanish.
const EDITABLE_TYPES: [string, string][] = [
  ["mga", "MGA"], ["mgu", "MGU"], ["broker", "Broker"], ["tpa", "TPA"],
];
const CURRENCIES: [string, string][] = [
  ["USD", "US Dollar"], ["GBP", "Pound"], ["EUR", "Euro"],
  ["CAD", "Canadian Dollar"], ["AUD", "Australian Dollar"],
];
// The API normalizes every stored/legacy role string down to this 2-role
// tenant vocabulary (kavachio_admin rows are filtered out server-side —
// never surfaced on a tenant's own Users screen).
const ROLE_LABEL: Record<string, string> = {
  carrier_admin: "Carrier",
  broker_admin: "Broker",
  operator: "Broker User",
};

// Single source of truth for a user's status bucket — used by both the
// Status filter and the badge, so they can never drift out of sync.
function statusKey(s: string): "active" | "invited" | "inactive" {
  if (s === "active") return "active";
  if (s === "pending" || s === "invited") return "invited";
  return "inactive";
}
const USER_STATUS_LABEL: Record<string, string> = { active: "Active", invited: "Invited", inactive: "Inactive" };
function userStatus(s: string) {
  const k = statusKey(s);
  if (k === "invited") return { cls: "b-warn", label: "Invited" };
  if (k === "inactive") return { cls: "b-mut", label: "Inactive" };
  return { cls: "b-ok", label: "Active" };
}
function itemStatus(s?: string | null) {
  const v = (s ?? "").toLowerCase();
  if (v === "active") return { cls: "b-ok", label: "Active" };
  return { cls: "b-mut", label: s || "—" };
}

// Both tabs fetch their full dataset in one API call (no server-side paging),
// so filtering and pagination happen client-side after fetch, same as Tenants.tsx.
const PAGE_SIZE = 10;

// TEMPORARILY HIDDEN (29 Sep 2026, at the user's request): the Recent File
// Submissions tab. `?tab=runs` falls back to Details while this is off. Flip
// to true to bring the tab back unchanged.
const SHOW_FILE_SUBMISSIONS = false;

export default function TenantDetail() {
  const { mga = "" } = useParams();
  const nav = useNavigate();
  const isAdmin = isKavachioAdmin();
  const [t, setT] = useState<TenantRow | null>(null);
  // `?tab=runs` opens straight on Recent File Submissions — the dashboard's
  // "Exceptions by Carrier" links here to show that carrier's flagged files.
  // Dropped from the address as soon as another tab is picked, so a refresh
  // stays where the user actually is.
  const [params, setParams] = useSearchParams();
  const [tab, setTabState] = useState<"details" | "users" | "pc" | "runs">(
    () => (SHOW_FILE_SUBMISSIONS && params.get("tab") === "runs" ? "runs" : "details"));
  const setTab = (next: "details" | "users" | "pc" | "runs") => {
    setTabState(next);
    if (params.has("tab") || params.has("from") || params.has("to")) {
      const p = new URLSearchParams(params);
      ["tab", "from", "to"].forEach(k => p.delete(k));
      setParams(p, { replace: true });
    }
  };
  // Org Details tab — an editable copy of the tenant's own fields, seeded from
  // `t` and PUT back on save.
  const [form, setForm] = useState<{ legal_name: string; tenant_type: string; currency: string } | null>(null);
  const [savingDetails, setSavingDetails] = useState(false);
  const [detailsMsg, setDetailsMsg] = useState<string | null>(null);
  const [detailsErr, setDetailsErr] = useState<string | null>(null);
  // Programs & contracts: clicking a program shows that program's contracts;
  // clicking it again clears the panel.
  //
  // The NAME is held beside the id, not looked up in the programmes on screen.
  // Programmes page now, so the selected one can be on a page nobody is
  // looking at — and a panel that emptied itself because the reader turned
  // the list behind it would look like the contracts had gone.
  const [selected, setSelected] = useState<{ id: number; name: string } | null>(null);
  const selectedProgramId = selected?.id ?? null;
  // The viewed programme's brokers, for the drawer — a short whole list,
  // paged here.
  const {
    page: brkPage, setPage: setBrkPage, items: brkItems, total: brkTotal,
    pageCount: brkPageCount, loading: brkLoading,
  } = useServerList<ProgBroker>(
    (page, pageSize) =>
      selectedProgramId == null
        ? Promise.resolve({ items: [], total: 0 })
        : api.get<ProgBroker[]>(`/programs/${selectedProgramId}/brokers`, { params: { mga } })
            .then(r => ({ items: r.data.slice((page - 1) * pageSize, page * pageSize),
                          total: r.data.length })),
    isAdmin ? `pbrokers|${selectedProgramId ?? ""}` : "disabled",
    DRAWER_PAGE,
  );
  // Escape closes the drawer.
  useEffect(() => {
    if (selectedProgramId == null) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setSelected(null); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedProgramId]);

  // Programme id → the brokers on it, for the table's Brokers column. The
  // carrier's whole directory in one read (a carrier holds a handful).
  const [brokersByProg, setBrokersByProg] = useState<Record<number, string[]>>({});
  useEffect(() => {
    if (!isAdmin || !mga) return;
    api.get<DirBroker[]>("/brokers", { params: { mga } })
      .then(r => {
        const m: Record<number, string[]> = {};
        for (const b of r.data)
          for (const p of b.programmes ?? []) (m[p.id] ??= []).push(b.legal_name);
        setBrokersByProg(m);
      })
      .catch(() => setBrokersByProg({}));
  }, [isAdmin, mga]);

  // Users tab filters — independent of the Recent runs tab's own filters below.
  const [userQ, setUserQ] = useState("");
  const [userRole, setUserRole] = useState("");
  const [userStatusFilter, setUserStatusFilter] = useState("");
  const userDq = useDebouncedValue(userQ, 300);
  // Recent runs tab filters.
  const [runQ, setRunQ] = useState("");
  // Pre-filled from the address when a link asks for particular days — the
  // platform dashboard's "files on this day" panel sends ?from=&to=.
  const dayParam = (k: string) => (/^\d{4}-\d{2}-\d{2}$/.test(params.get(k) ?? "") ? params.get(k)! : "");
  const [runDateFrom, setRunDateFrom] = useState(() => dayParam("from"));
  const [runDateTo, setRunDateTo] = useState(() => dayParam("to"));
  const runDq = useDebouncedValue(runQ, 300);

  useEffect(() => {
    if (!mga || !isAdmin) return;
    api.get<TenantRow>(`/tenants/${mga}`)
      .then(r => setT(r.data)).catch(() => {});
    // Programmes, contracts and templates are fetched a page at a time
    // further down. This used to read all three whole, and the contracts one
    // programme at a time — one request per programme on the carrier, before
    // anybody had clicked the tab they are on.
  }, [mga, isAdmin]);

  // Seed / re-seed the Details form whenever the tenant record changes (initial
  // load, or after a save returns the updated record).
  useEffect(() => {
    if (!t) return;
    setForm({
      legal_name: t.legal_name ?? t.name ?? "",
      tenant_type: t.tenant_type ?? "",
      currency: t.currency ?? "USD",
    });
  }, [t]);

  async function saveDetails() {
    if (!form || !mga) return;
    setSavingDetails(true); setDetailsErr(null); setDetailsMsg(null);
    try {
      const { data } = await api.put<TenantRow>(`/tenants/${mga}`, {
        legal_name: form.legal_name.trim(),
        tenant_type: form.tenant_type || null,
        currency: form.currency || null,
      });
      setT(prev => (prev ? { ...prev, ...data } : data));
      setDetailsMsg("Changes saved.");
    } catch (e: any) {
      setDetailsErr(e?.response?.data?.detail ?? "Could not save changes.");
    } finally { setSavingDetails(false); }
  }

  const iso = (d: Date | null) => (d ? d.toISOString() : "");

  // TRUE server-side pagination for both tabs — the backend filters + pages;
  // we send the current filters and receive just this page + the matching
  // total. Each tab's "disabled" filterKey (when not admin) mirrors Tenants.tsx.
  const userFilterKey = `${userDq}|${userRole}|${userStatusFilter}`;
  const {
    page: userPage, setPage: setUserPage, items: userItems, total: userTotal,
    pageCount: userPageCount,
  } = useServerList<U>(
    (page, pageSize) =>
      api.get<{ items: U[]; total: number }>("/users", {
        params: {
          mga, page, page_size: pageSize,
          q: userDq || undefined, role: userRole || undefined, status: userStatusFilter || undefined,
        },
      }).then(r => r.data),
    isAdmin ? userFilterKey : "disabled",
    PAGE_SIZE,
  );
  const userPageRows = userItems;
  const userFiltersActive = userQ !== "" || userRole !== "" || userStatusFilter !== "";
  function clearUserFilters() { setUserQ(""); setUserRole(""); setUserStatusFilter(""); }

  // --- Programs & Contracts tab: three lists, three pagers ---------------
  // All three were read whole before, and the contracts one request PER
  // PROGRAMME on top of that. Each now asks the server for its own page.

  const {
    page: progPage, setPage: setProgPage, items: progItems, total: progTotal,
    pageCount: progPageCount,
  } = useServerList<Program>(
    (page, pageSize) =>
      api.get<{ items: Program[]; total: number }>("/programs", {
        params: { mga, page, page_size: pageSize },
      }).then(r => r.data),
    isAdmin ? "programs" : "disabled",
    PAGE_SIZE,
  );

  // Contracts follow the SELECTED programme, so its id is the filter key —
  // picking another programme snaps back to page 1, which is what turning to
  // a different list should do. With nothing picked there is nothing to ask
  // for, and the panel says so rather than showing the last programme's.
  const {
    page: conPage, setPage: setConPage, items: conItems, total: conTotal,
    pageCount: conPageCount, loading: conLoading,
  } = useServerList<Contract>(
    (page, pageSize) =>
      selectedProgramId == null
        ? Promise.resolve({ items: [], total: 0 })
        : api.get<{ items: Contract[]; total: number }>(
            `/programs/${selectedProgramId}/contracts`,
            { params: { page, page_size: pageSize } },
          ).then(r => r.data),
    isAdmin ? `contracts|${selectedProgramId ?? ""}` : "disabled",
    DRAWER_PAGE,
  );


  const runFilterKey = `${runDq}|${runDateFrom}|${runDateTo}`;
  const {
    page: runPage, setPage: setRunPage, items: runItems, total: runTotal,
    pageCount: runPageCount,
  } = useServerList<Run>(
    (page, pageSize) =>
      api.get<{ items: Run[]; total: number }>("/direct/runs", {
        params: {
          mga, page, page_size: pageSize,
          q: runDq || undefined,
          date_from: iso(localDayStart(runDateFrom)) || undefined,
          date_to: iso(localDayEnd(runDateTo)) || undefined,
        },
      }).then(r => r.data),
    isAdmin ? runFilterKey : "disabled",
    PAGE_SIZE,
  );
  const runPageRows = runItems;
  const runFiltersActive = runQ !== "" || runDateFrom !== "" || runDateTo !== "";
  function clearRunFilters() { setRunQ(""); setRunDateFrom(""); setRunDateTo(""); }

  if (!isAdmin) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Carrier</h2></div></div>
        <div className="note warn" style={{ maxWidth: 560 }}>
          This screen is restricted to Kavachio platform admins.
        </div>
      </div></div>
    );
  }

  const name = t?.name || mga;
  const subtitle = [t?.code || mga,
    t?.tenant_type ? (TYPE_LABEL[t.tenant_type] ?? t.tenant_type) : null].filter(Boolean).join(" · ");

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2 style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {name}
              <span className={`badge ${t?.is_active === false ? "b-mut" : "b-ok"}`} style={{ verticalAlign: "middle" }}>
                <span className="d" />{t?.is_active === false ? "Inactive" : "Active"}
              </span>
            </h2>
            <p className="mono">{subtitle}</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/tenants")}>← Carriers</button>
          </div>
        </div>

        <div className="tabs">
          <button className={tab === "details" ? "on" : ""} onClick={() => setTab("details")}>
            Details
          </button>
          <button className={tab === "users" ? "on" : ""} onClick={() => setTab("users")}>
            Users ({userTotal})
          </button>
          <button className={tab === "pc" ? "on" : ""} onClick={() => setTab("pc")}>
            Programs &amp; Contracts
          </button>
          {SHOW_FILE_SUBMISSIONS && (
            <button className={tab === "runs" ? "on" : ""} onClick={() => setTab("runs")}>
              Recent File Submissions ({runTotal})
            </button>
          )}
        </div>

        {/* Details — the organization's own fields (captured at creation), editable */}
        {tab === "details" && form && (
          <div className="grid g-2">
            <div className="card pad">
              <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Organization</h3>
              {detailsErr && <div className="note warn" style={{ marginBottom: 14 }}>{detailsErr}</div>}
              {detailsMsg && <div className="note ok" style={{ marginBottom: 14 }}>{detailsMsg}</div>}
              <div className="field">
                <label>Organization name</label>
                <input value={form.legal_name}
                  onChange={e => setForm(f => f && { ...f, legal_name: e.target.value })} />
              </div>
              <div className="field">
                <label>Type</label>
                <select value={form.tenant_type}
                  onChange={e => setForm(f => f && { ...f, tenant_type: e.target.value })}>
                  {!EDITABLE_TYPES.some(([v]) => v === form.tenant_type) && (
                    <option value={form.tenant_type}>
                      {form.tenant_type ? (TYPE_LABEL[form.tenant_type] ?? form.tenant_type) : "—"}
                    </option>
                  )}
                  {EDITABLE_TYPES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Base currency</label>
                <select value={form.currency}
                  onChange={e => setForm(f => f && { ...f, currency: e.target.value })}>
                  {CURRENCIES.map(([v, l]) => <option key={v} value={v}>{v} — {l}</option>)}
                </select>
              </div>
              <div style={{ marginTop: 18 }}>
                <button className="btn pri" onClick={saveDetails}
                  disabled={savingDetails || !form.legal_name.trim()}>
                  {savingDetails ? "Saving…" : "Save Changes"}
                </button>
              </div>
            </div>
            <div className="card pad">
              <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Account</h3>
              <div style={{ display: "grid", gap: 14 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <span className="muted">Account code</span><span className="mono">{t?.code || mga}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "center" }}>
                  <span className="muted">Status</span>
                  <span className={`badge ${t?.is_active === false ? "b-mut" : "b-ok"}`}>
                    <span className="d" />{t?.is_active === false ? "Inactive" : "Active"}
                  </span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <span className="muted">Users</span><span>{userTotal}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <span className="muted">Created</span><span>{fmtDateTime(t?.created_at)}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <span className="muted">Last modified</span><span>{fmtDateTime(t?.modified_at)}</span>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Users */}
        {tab === "users" && (
          <div className="card">
            <ListFilterBar
              search={{ value: userQ, onChange: setUserQ, placeholder: "Search name or email…" }}
              selects={[
                {
                  key: "role", ariaLabel: "Filter by role", value: userRole, onChange: setUserRole,
                  options: [{ value: "", label: "All Roles" },
                    ...Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))],
                },
                {
                  key: "status", ariaLabel: "Filter by status", value: userStatusFilter, onChange: setUserStatusFilter,
                  options: [{ value: "", label: "All Statuses" },
                    ...Object.entries(USER_STATUS_LABEL).map(([value, label]) => ({ value, label }))],
                },
              ]}
              onClear={clearUserFilters}
              active={userFiltersActive}
            />
            <div className="tbl-wrap">
              <table>
                <thead><tr><th>Name</th><th>Role</th><th>Status</th><th>Last Sign-In</th></tr></thead>
                <tbody>
                  {userPageRows.map(u => {
                    const us = userStatus(u.status);
                    const role = normalizeRole(u.role);
                    return (
                      <tr key={u.id}>
                        <td><b>{u.full_name}</b><div className="sub">{u.email}</div></td>
                        <td><span className={`badge ${role === "carrier_admin" ? "b-info" : "b-mut"}`}>
                          <span className="d" />
                          {role === "carrier_admin" && !u.is_owner ? "Carrier User" : (ROLE_LABEL[role] ?? role)}
                          </span></td>
                        <td><span className={`badge ${us.cls}`}><span className="d" />{us.label}</span></td>
                        <td className="muted">{fmtDateTime(u.last_login_at)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {userTotal === 0 && !userFiltersActive && <div className="empty">No users in this organization.</div>}
              {userTotal === 0 && userFiltersActive && <div className="empty">No users match the filters.</div>}
            </div>
            {userTotal > 0 && (
              <Pagination page={userPage} pageCount={userPageCount} pageSize={PAGE_SIZE}
                totalItems={userTotal} onPageChange={setUserPage} noun="users" />
            )}
          </div>
        )}

        {/* Programs & contracts */}
        {/* Programs & contracts — ONE table of programmes. "View" opens a
            right-hand drawer with that programme's contracts and brokers,
            each paged on its own. */}
        {tab === "pc" && (() => {
          const L: React.CSSProperties = { textAlign: "left" };
          return (
          <>
          <div className="card">
            <div className="card-h">
              <h3>Programs &amp; Contracts</h3><span className="sub">{progTotal} programs</span>
            </div>
            <div className="tbl-wrap">
              <table>
                <thead>
                  <tr><th>Program</th><th>Brokers</th>
                    <th className="r">Contracts</th><th className="r">Status</th><th className="r" /></tr>
                </thead>
                <tbody>
                  {progItems.map(p => {
                    const st = itemStatus(p.status);
                    const count = p.contract_count ?? 0;
                    const brokers = brokersByProg[p.id] ?? [];
                    return (
                      <tr key={p.id} className={p.id === selectedProgramId ? "sel" : undefined}>
                        <td><b>{p.name}</b>{p.product_line && <div className="sub">{p.product_line}</div>}</td>
                        <td>{brokers.length ? brokers.join(", ")
                          : <span className="muted">No broker yet</span>}</td>
                        <td className="r">{count}</td>
                        <td className="r"><span className={`badge ${st.cls}`}><span className="d" />{st.label}</span></td>
                        <td className="r">
                          <button type="button" className="btn sm"
                            onClick={() => setSelected({ id: p.id, name: p.name })}>View</button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {progTotal === 0 && <div className="empty">No programs yet.</div>}
            </div>
            {progTotal > 0 && (
              <Pagination page={progPage} pageCount={progPageCount} pageSize={PAGE_SIZE}
                totalItems={progTotal} onPageChange={setProgPage} noun="programs" />
            )}
          </div>

          {/* ---- The programme drawer ---- */}
          <div className={`scrim${selected ? " on" : ""}`} onClick={() => setSelected(null)} />
          <aside className={`drawer wide${selected ? " on" : ""}`} aria-hidden={!selected}
            aria-label={selected ? `${selected.name} — contracts and brokers` : undefined}>
            <div className="drawer-h">
              <div>
                <h4>{selected?.name ?? ""}</h4>
                <div style={{ fontSize: 12.5, color: "var(--p-muted)" }}>
                  {selected ? `${conTotal} contract${conTotal === 1 ? "" : "s"} · ${brkTotal} broker${brkTotal === 1 ? "" : "s"}` : " "}
                </div>
              </div>
              <button type="button" className="closeb" aria-label="Close" onClick={() => setSelected(null)}>×</button>
            </div>
            <div className="drawer-b" style={{ padding: 0 }}>
              {/* Contracts */}
              <div className="card-h" style={{ borderTop: 0 }}>
                <h3>Contracts</h3><span className="sub">{conTotal}</span>
              </div>
              {conLoading ? <div className="muted" style={{ padding: "14px 20px" }}>Loading…</div>
                : conTotal === 0 ? <div className="empty">No contracts on this program yet.</div> : (
                <div className="tbl-wrap">
                  <table>
                    <thead><tr><th style={L}>Contract</th><th style={L}>Broker</th><th className="r">Status</th></tr></thead>
                    <tbody>
                      {conItems.map(c => {
                        const cs = contractBadge(c.status);
                        const from = fmtDay(c.inception_dt), to = fmtDay(c.expiry_dt);
                        return (
                          <tr key={c.id}>
                            <td style={L}><b>{c.name || c.filename || `Contract #${c.id}`}</b>
                              <div className="sub">
                                {from || to ? `${from ?? "—"} – ${to ?? "—"}` : "No term set"}
                                {" · "}{c.clause_count ?? 0} clause{(c.clause_count ?? 0) === 1 ? "" : "s"}
                              </div></td>
                            <td style={L}>{c.broker_name ?? <span className="muted">Held by the carrier</span>}</td>
                            <td className="r"><span className={`badge ${cs.cls}`}><span className="d" />{cs.label}</span></td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              {conTotal > DRAWER_PAGE && (
                <Pagination page={conPage} pageCount={conPageCount} pageSize={DRAWER_PAGE}
                  totalItems={conTotal} onPageChange={setConPage} noun="contracts" />
              )}

              {/* Brokers */}
              <div className="card-h" style={{ marginTop: 10, borderTop: "1px solid var(--p-border)" }}>
                <h3>Brokers</h3><span className="sub">{brkTotal}</span>
              </div>
              {brkLoading ? <div className="muted" style={{ padding: "14px 20px" }}>Loading…</div>
                : brkTotal === 0 ? <div className="empty">No brokers on this program yet.</div> : (
                <div className="tbl-wrap">
                  <table>
                    <thead><tr><th style={L}>Broker</th><th className="r">Contracts</th><th className="r">Status</th></tr></thead>
                    <tbody>
                      {brkItems.map(b => {
                        const bs = linkBadge(b.status);
                        return (
                          <tr key={b.id}>
                            <td style={L}><b>{b.legal_name}</b></td>
                            <td className="r muted">{b.contract_count ?? 0}</td>
                            <td className="r"><span className={`badge ${bs.cls}`}><span className="d" />{bs.label}</span></td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
              {brkTotal > DRAWER_PAGE && (
                <Pagination page={brkPage} pageCount={brkPageCount} pageSize={DRAWER_PAGE}
                  totalItems={brkTotal} onPageChange={setBrkPage} noun="brokers" />
              )}
            </div>
          </aside>
          </>
          );
        })()}

        {/* Recent runs */}
        {tab === "runs" && (
          <div className="card">
            <ListFilterBar
              search={{ value: runQ, onChange: setRunQ, placeholder: "Search program or file…" }}
              dateRange={{ from: runDateFrom, onFromChange: setRunDateFrom, to: runDateTo, onToChange: setRunDateTo }}
              onClear={clearRunFilters}
              active={runFiltersActive}
            />
            <div className="tbl-wrap">
              <table>
                <thead><tr><th>Program</th><th>Run</th><th>Processed</th><th>Result</th></tr></thead>
                <tbody>
                  {runPageRows.map(r => {
                    const exc = r.exception_count ?? 0;
                    const badge = exc > 0
                      ? { cls: "b-warn", label: `${exc} Exception${exc === 1 ? "" : "s"}` }
                      : { cls: "b-ok", label: "Clean" };
                    return (
                      <tr key={r.landing_id}>
                        <td>{r.program_name ?? "—"}</td>
                        <td className="mono">{r.source_filename || `#${r.landing_id}`}</td>
                        <td className="muted">{fmtDateTime(r.created_at)}</td>
                        <td><span className={`badge ${badge.cls}`}><span className="d" />{badge.label}</span></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {runTotal === 0 && !runFiltersActive && <div className="empty">No runs yet.</div>}
              {runTotal === 0 && runFiltersActive && <div className="empty">No runs match the filters.</div>}
            </div>
            {runTotal > 0 && (
              <Pagination page={runPage} pageCount={runPageCount} pageSize={PAGE_SIZE}
                totalItems={runTotal} onPageChange={setRunPage} noun="runs" />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
