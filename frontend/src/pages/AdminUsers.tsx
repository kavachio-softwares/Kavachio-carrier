/**
 * Users & Roles, seen from the platform.
 *
 * Kavachio creates exactly ONE login per carrier: their first admin. That admin
 * invites their own colleagues and their brokers, and each broker adds its own
 * operators. So this screen is deliberately READ-ONLY — none of these accounts
 * is Kavachio's to change. It exists so that when someone calls, the platform
 * can say who they are and whether they can get in.
 */
import { useState } from "react";
import { api } from "../api/client";
import { isKavachioAdmin } from "../auth";
import { fmtDate, fmtDateTime } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

type Row = {
  id: number; full_name: string; email: string;
  role: string; status: string;
  org_name: string; org_kind: "kavachio" | "carrier" | "broker";
  created_at?: string | null; last_login_at?: string | null;
};
type Counts = {
  total: number; kavachio: number; carrier_users: number;
  broker_users: number; operators: number; never_signed_in: number;
  carriers: number; brokers: number;
};

const ROLE_LABEL: Record<string, string> = {
  kavachio_admin: "Kavachio Admin",
  carrier_admin: "Carrier Admin",
  broker_admin: "Broker Admin",
  operator: "Operator",
};
const STATUS_LABEL: Record<string, string> = {
  active: "Active", invited: "Invited", inactive: "Inactive",
};
function statusBadge(s: string) {
  if (s === "active") return { cls: "b-ok", label: "Active" };
  if (s === "invited" || s === "pending") return { cls: "b-warn", label: "Invited" };
  return { cls: "b-mut", label: "Inactive" };
}

const PAGE_SIZE = 10;

export default function AdminUsers() {
  const isAdmin = isKavachioAdmin();
  const [q, setQ] = useState("");
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const dq = useDebouncedValue(q, 300);

  const { page, setPage, items, total, extra, pageCount } =
    useServerList<Row, { counts: Counts }>(
      (page, pageSize) =>
        api.get<{ items: Row[]; total: number; counts: Counts }>("/admin/users", {
          params: {
            page, page_size: pageSize,
            q: dq || undefined, role: role || undefined, status: status || undefined,
          },
        }).then(r => r.data),
      `${dq}|${role}|${status}`,
      PAGE_SIZE,
    );

  const c = extra?.counts;
  const filtersActive = q !== "" || role !== "" || status !== "";
  function clearFilters() { setQ(""); setRole(""); setStatus(""); }

  if (!isAdmin) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Users &amp; Roles</h2></div></div>
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
            <h2>Users &amp; Roles</h2>
            <p>
              Everyone who can sign in to Kavachio, which company they work for,
              and what they are allowed to do.
            </p>
          </div>
          <div className="actions">
            <span className="badge b-mut"><span className="d" />View only</span>
          </div>
        </div>

    

        {c && (
          <div className="tiles five" style={{ marginBottom: 18 }}>
            <Tile k="Users" v={c.total}
              foot={c.never_signed_in > 0
                ? `${c.never_signed_in} ${c.never_signed_in === 1 ? "has" : "have"} not signed in yet`
                : "all have signed in"} />
            <Tile k="Kavachio Admin" v={c.kavachio}
              foot={c.kavachio === 1 ? "the only Kavachio account" : "Kavachio accounts"} />
            <Tile k="Carrier users" v={c.carrier_users}
              foot={`across ${c.carriers} ${c.carriers === 1 ? "carrier" : "carriers"}`} />
            <Tile k="Broker users" v={c.broker_users}
              foot={`across ${c.brokers} ${c.brokers === 1 ? "broker" : "brokers"}`} />
            <Tile k="Operators" v={c.operators} foot="added by their brokers" />
          </div>
        )}

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search people…" }}
            selects={[
              {
                key: "role", ariaLabel: "Filter by role", value: role, onChange: setRole,
                options: [{ value: "", label: "All roles" },
                  ...Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))],
              },
              {
                key: "status", ariaLabel: "Filter by status", value: status, onChange: setStatus,
                options: [{ value: "", label: "All statuses" },
                  ...Object.entries(STATUS_LABEL).map(([value, label]) => ({ value, label }))],
              },
            ]}
            onClear={clearFilters}
            active={filtersActive}
          />

          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th><th>Organisation</th><th>Role</th>
                  <th>Status</th><th>Created</th><th>Last sign-in</th>
                </tr>
              </thead>
              <tbody>
                {items.map(u => {
                  const sb = statusBadge(u.status);
                  return (
                    <tr key={u.id}>
                      <td>
                        <b>{u.full_name}</b>
                        <div className="sub">{u.email}</div>
                      </td>
                      <td>
                        {u.org_name}
                        {/* Which side of the platform they sit on. A broker
                            works for several carriers, so its name alone does
                            not say whose book this is. */}
                        <div className="sub">
                          {u.org_kind === "kavachio" ? "the platform"
                            : u.org_kind === "broker" ? "broker" : "carrier"}
                        </div>
                      </td>
                      <td>{ROLE_LABEL[u.role] ?? u.role}</td>
                      <td><span className={`badge ${sb.cls}`}><span className="d" />{sb.label}</span></td>
                      <td className="muted">{fmtDate(u.created_at)}</td>
                      <td className="muted">
                        {u.last_login_at ? fmtDateTime(u.last_login_at) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {total === 0 && (
              <div className="empty">
                {filtersActive ? "Nobody matches the filters." : "No users yet."}
              </div>
            )}
          </div>
          {total > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={total} onPageChange={setPage} noun="people" />
          )}
        </div>
      </div>
    </div>
  );
}

function Tile({ k, v, foot }: { k: string; v: number; foot: string }) {
  return (
    <div className="tile">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
      <div className="foot">{foot}</div>
    </div>
  );
}
