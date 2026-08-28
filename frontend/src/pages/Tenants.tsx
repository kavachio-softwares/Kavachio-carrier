import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { isKavachioAdmin } from "../auth";
import { fmtDate, localDayStart, localDayEnd } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

type Tenant = {
  mga: string; name: string; code: string;
  tenant_type?: string | null; users: number; setups: number; is_active: boolean;
  pending_invites?: number; active_users?: number; created_at?: string | null;
};

const TYPE_LABEL: Record<string, string> = {
  carrier: "Carrier", mga: "MGA", mgu: "MGU", broker: "Broker",
  tpa: "TPA", reinsurer: "Reinsurer",
};
// The type FILTER only offers the types a tenant can actually be provisioned as
// (mirrors the backend allow-list + Add Broker form). carrier/reinsurer stay in
// TYPE_LABEL so any legacy row of that type still renders a proper label.
const FILTER_TYPES = ["mga", "mgu", "broker", "tpa"];

type Status = "active" | "inactive" | "invited";
const STATUS_LABEL: Record<Status, string> = { active: "Active", inactive: "Inactive", invited: "Invited" };

// Status is derived server-side from the same inputs — is_active + pending
// invites + active users — but we still compute the badge locally from the
// returned fields. "Invited" applies ONLY while nobody has signed in yet
// (pending invites AND zero active users); a broker with any active user stays
// "active" even with outstanding invites. Mirrors the /tenants list filter.
function tenantStatus(t: Tenant): Status {
  if (!t.is_active) return "inactive";
  if ((t.pending_invites ?? 0) > 0 && (t.active_users ?? 0) === 0) return "invited";
  return "active";
}
function statusBadge(s: Status) {
  if (s === "inactive") return { cls: "b-mut", label: "Inactive" };
  if (s === "invited") return { cls: "b-warn", label: "Invited" };
  return { cls: "b-ok", label: "Active" };
}

const PAGE_SIZE = 10;
const iso = (d: Date | null) => (d ? d.toISOString() : "");

export default function Tenants() {
  const nav = useNavigate();
  const isAdmin = isKavachioAdmin();

  const [q, setQ] = useState("");
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const dq = useDebouncedValue(q, 300);

  // TRUE server-side pagination: the backend filters + pages; we send the
  // current filters and receive just this page + the matching total.
  const filterKey = `${dq}|${type}|${status}|${dateFrom}|${dateTo}`;
  const { page, setPage, items, total, loading, pageCount } = useServerList<Tenant>(
    (page, pageSize) =>
      api.get<{ items: Tenant[]; total: number }>("/tenants", {
        params: {
          page, page_size: pageSize,
          q: dq || undefined,
          tenant_type: type || undefined,
          status: status || undefined,
          date_from: iso(localDayStart(dateFrom)) || undefined,
          date_to: iso(localDayEnd(dateTo)) || undefined,
        },
      }).then(r => r.data),
    isAdmin ? filterKey : "disabled",
    PAGE_SIZE,
  );

  const filtersActive = q !== "" || type !== "" || status !== "" || dateFrom !== "" || dateTo !== "";
  function clearFilters() { setQ(""); setType(""); setStatus(""); setDateFrom(""); setDateTo(""); }

  if (!isAdmin) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Brokers</h2></div></div>
        <div className="note warn" style={{ maxWidth: 560 }}>
          This screen is restricted to Kavachio platform admins.
        </div>
      </div></div>
    );
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Brokers</h2>
            <p>Every organization on the platform.</p>
          </div>
          <div className="actions">
            <button className="btn pri" onClick={() => nav("/tenants/new")}>＋ Add Broker</button>
          </div>
        </div>

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search organizations…" }}
            selects={[
              {
                key: "type", ariaLabel: "Filter by type", value: type, onChange: setType,
                options: [{ value: "", label: "All Types" },
                  ...FILTER_TYPES.map(value => ({ value, label: TYPE_LABEL[value] }))],
              },
              {
                key: "status", ariaLabel: "Filter by status", value: status, onChange: setStatus,
                options: [{ value: "", label: "All Statuses" },
                  ...(Object.entries(STATUS_LABEL) as [Status, string][])
                    .filter(([value]) => value !== "inactive")
                    .map(([value, label]) => ({ value, label }))],
              },
            ]}
            dateRange={{ from: dateFrom, onFromChange: setDateFrom, to: dateTo, onToChange: setDateTo }}
            onClear={clearFilters}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Organization</th><th>Type</th><th>Users</th>
                  <th>Created</th><th>Status</th><th></th>
                </tr>
              </thead>
              <tbody>
                {items.map(t => {
                  const sb = statusBadge(tenantStatus(t));
                  return (
                    <tr key={t.mga}>
                      <td>
                        <b>{t.name}</b>
                        <div className="sub mono">{t.code}</div>
                      </td>
                      <td>{t.tenant_type ? (TYPE_LABEL[t.tenant_type] ?? t.tenant_type) : "—"}</td>
                      <td>{t.users}</td>
                      <td className="muted">{fmtDate(t.created_at)}</td>
                      <td><span className={`badge ${sb.cls}`}><span className="d" />{sb.label}</span></td>
                      <td className="r">
                        <span className="linkish" onClick={() => nav(`/tenants/${encodeURIComponent(t.mga)}`)}>View</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!loading && total === 0 && (
              <div className="empty">{filtersActive ? "No brokers match the filters." : "No brokers found."}</div>
            )}
          </div>
          {!loading && total > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={total} onPageChange={setPage} noun="brokers" />
          )}
        </div>

      </div>
    </div>
  );
}
