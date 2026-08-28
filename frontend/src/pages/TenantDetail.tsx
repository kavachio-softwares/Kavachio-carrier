import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
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
type U = { id: number; email: string; full_name: string; role: string; status: string; last_login_at?: string | null };
type Program = { id: number; name: string; product_line?: string | null; status?: string | null };
type Contract = {
  id: number; filename?: string | null; status?: string | null;
  created_at?: string | null; clause_count?: number;
  program_id?: number; program_name?: string;
};
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
const ROLE_LABEL: Record<string, string> = { tenant_admin: "Admin", tenant_user: "Operator" };

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

export default function TenantDetail() {
  const { mga = "" } = useParams();
  const nav = useNavigate();
  const isAdmin = isKavachioAdmin();
  const [t, setT] = useState<TenantRow | null>(null);
  const [programs, setPrograms] = useState<Program[]>([]);
  const [contracts, setContracts] = useState<Contract[]>([]);
  const [tab, setTab] = useState<"details" | "users" | "pc" | "runs">("details");
  // Org Details tab — an editable copy of the tenant's own fields, seeded from
  // `t` and PUT back on save.
  const [form, setForm] = useState<{ legal_name: string; tenant_type: string; currency: string } | null>(null);
  const [savingDetails, setSavingDetails] = useState(false);
  const [detailsMsg, setDetailsMsg] = useState<string | null>(null);
  const [detailsErr, setDetailsErr] = useState<string | null>(null);
  // Programs & contracts: clicking a program filters the Contracts panel to
  // just that program's contracts; null (or clicking it again) shows all.
  const [selectedProgramId, setSelectedProgramId] = useState<number | null>(null);

  // Users tab filters — independent of the Recent runs tab's own filters below.
  const [userQ, setUserQ] = useState("");
  const [userRole, setUserRole] = useState("");
  const [userStatusFilter, setUserStatusFilter] = useState("");
  const userDq = useDebouncedValue(userQ, 300);
  // Recent runs tab filters.
  const [runQ, setRunQ] = useState("");
  const [runDateFrom, setRunDateFrom] = useState("");
  const [runDateTo, setRunDateTo] = useState("");
  const runDq = useDebouncedValue(runQ, 300);

  useEffect(() => {
    if (!mga || !isAdmin) return;
    api.get<TenantRow>(`/tenants/${mga}`)
      .then(r => setT(r.data)).catch(() => {});
    api.get<Program[]>("/programs", { params: { mga } }).then(async r => {
      setPrograms(r.data);
      const lists = await Promise.all(
        r.data.map(p => api.get<Contract[]>(`/programs/${p.id}/contracts`)
          .then(cr => cr.data.map(c => ({ ...c, program_id: p.id, program_name: p.name })))
          .catch(() => [])),
      );
      setContracts(lists.flat());
    }).catch(() => { setPrograms([]); setContracts([]); });
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
        <div className="page-head"><div className="t"><h2>Broker</h2></div></div>
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
            <button className="btn" onClick={() => nav("/tenants")}>← Brokers</button>
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
          <button className={tab === "runs" ? "on" : ""} onClick={() => setTab("runs")}>
            Recent Runs ({runTotal})
          </button>
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
                        <td><span className={`badge ${role === "tenant_admin" ? "b-info" : "b-mut"}`}>
                          <span className="d" />{ROLE_LABEL[role] ?? role}</span></td>
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
        {tab === "pc" && (() => {
          const selectedProgram = programs.find(p => p.id === selectedProgramId) ?? null;
          // Nothing shows on first load — a program must be picked before its
          // contracts are shown, so contracts are never viewed without knowing
          // which program they belong to.
          const visibleContracts = selectedProgram
            ? contracts.filter(c => c.program_id === selectedProgram.id)
            : [];
          return (
          <div className="grid g-2">
            <div className="card">
              <div className="card-h"><h3>Programs</h3><span className="sub">{programs.length}</span></div>
              <div className="tbl-wrap">
                <table>
                  <tbody>
                    {programs.map(p => {
                      const st = itemStatus(p.status);
                      const count = contracts.filter(c => c.program_id === p.id).length;
                      const isSel = p.id === selectedProgramId;
                      return (
                        <tr key={p.id} className={`click${isSel ? " sel" : ""}`}
                          onClick={() => setSelectedProgramId(cur => cur === p.id ? null : p.id)}>
                          <td><b>{p.name}</b>{p.product_line && <div className="sub">{p.product_line}</div>}</td>
                          <td className="r muted">{count} contract{count === 1 ? "" : "s"}</td>
                          <td className="r"><span className={`badge ${st.cls}`}><span className="d" />{st.label}</span></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {programs.length === 0 && <div className="empty">No programs yet.</div>}
              </div>
            </div>
            <div className="card">
              <div className="card-h">
                <h3>Contracts</h3>
                {selectedProgram && (
                  <>
                    <span className="sub">{visibleContracts.length} · {selectedProgram.name}</span>
                    <div className="right">
                      <span className="linkish" onClick={() => setSelectedProgramId(null)}>Clear Selection</span>
                    </div>
                  </>
                )}
              </div>
              <div className="tbl-wrap">
                <table>
                  <tbody>
                    {visibleContracts.map(c => {
                      const st = itemStatus(c.status);
                      return (
                        <tr key={c.id}>
                          <td><b>{c.filename || `Contract #${c.id}`}</b>
                            <div className="sub">
                              {c.program_name ?? "—"} · {fmtDateTime(c.created_at)} · {c.clause_count ?? 0} clause{(c.clause_count ?? 0) === 1 ? "" : "s"}
                            </div></td>
                          <td className="r"><span className={`badge ${st.cls}`}><span className="d" />{st.label}</span></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                {visibleContracts.length === 0 && (
                  <div className="empty">
                    {selectedProgram ? "No contracts for this program." : "Select a program to see its contracts."}
                  </div>
                )}
              </div>
            </div>
          </div>
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
