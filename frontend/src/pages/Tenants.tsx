import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { isKavachioAdmin } from "../auth";
import { fmtDate, localDayStart, localDayEnd } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { InviteSentModal } from "../components/InviteSentModal";

type Tenant = {
  mga: string; name: string; code: string;
  tenant_type?: string | null; users: number; setups: number; is_active: boolean;
  programmes?: number; brokers?: number;
  pending_invites?: number; active_users?: number; created_at?: string | null;
};

const TYPE_LABEL: Record<string, string> = {
  carrier: "Carrier", mga: "MGA", mgu: "MGU", broker: "Broker",
  tpa: "TPA", reinsurer: "Reinsurer",
};
// Kavachio provisions carriers only, so a type filter would be a dropdown of
// one. TYPE_LABEL stays for any legacy row of another type, which still needs
// a readable label in the table.

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
  const [status, setStatus] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const dq = useDebouncedValue(q, 300);
  // Resend-invite feedback: a SUCCESS opens the same popup as a first-time
  // invite (Create Carrier); a failure falls back to an inline banner. `resending`
  // is the row whose request is still in flight, so its link can't be
  // double-clicked.
  const [resent, setResent] = useState<{ org: string; email: string } | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "warn"; text: string } | null>(null);
  const [resending, setResending] = useState<string | null>(null);

  // TRUE server-side pagination: the backend filters + pages; we send the
  // current filters and receive just this page + the matching total.
  const filterKey = `${dq}|${status}|${dateFrom}|${dateTo}`;
  const { page, setPage, items, total, loading, pageCount, reload } = useServerList<Tenant>(
    (page, pageSize) =>
      api.get<{ items: Tenant[]; total: number }>("/tenants", {
        params: {
          page, page_size: pageSize,
          q: dq || undefined,
          status: status || undefined,
          date_from: iso(localDayStart(dateFrom)) || undefined,
          date_to: iso(localDayEnd(dateTo)) || undefined,
        },
      }).then(r => r.data),
    isAdmin ? filterKey : "disabled",
    PAGE_SIZE,
  );

  const filtersActive = q !== "" || status !== "" || dateFrom !== "" || dateTo !== "";
  function clearFilters() { setQ(""); setStatus(""); setDateFrom(""); setDateTo(""); }

  // Resend the onboarding link for a carrier that never completed onboarding —
  // the invite mail was deleted, lost or has expired. The server issues a FRESH
  // token to every user of that org still on a pending invite (which also
  // invalidates the old link) and re-sends the set-password email.
  async function resendInvite(t: Tenant) {
    setMsg(null); setResending(t.mga);
    try {
      const { data } = await api.post<{ sent: number; emails: string[] }>(
        `/tenants/${encodeURIComponent(t.mga)}/resend-invite`);
      setResent({ org: t.name, email: (data?.emails ?? []).join(", ") });
      // Re-issuing resets status to "invited" — refetch so the badge and the
      // pending count on screen match what the server now holds.
      reload();
    } catch (e: any) {
      setMsg({ kind: "warn", text: e?.response?.data?.detail
        ?? `Couldn't resend the invite for ${t.name} — please try again.` });
    } finally { setResending(null); }
  }

  if (!isAdmin) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Carriers</h2></div></div>
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
            <h2>Carriers</h2>
            <p>
              All the insurance companies using Kavachio. When you add one, we
              email its first admin an invitation to join.
            </p>
          </div>
          <div className="actions">
            <button className="btn pri" onClick={() => nav("/tenants/new")}>＋ Create Carrier</button>
          </div>
        </div>

        {msg && (
          <div className={`note ${msg.kind}`} style={{ marginBottom: 14, maxWidth: 640 }}>
            {msg.text}
          </div>
        )}

        {resent && (
          <InviteSentModal
            title="Invite re-sent"
            message={`${resent.org} can finish setting up their account.`}
            email={resent.email}
            note="The earlier link no longer works — they'll set a password with this one."
            onDone={() => setResent(null)}
          />
        )}

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search carriers…" }}
            selects={[
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
                  <th>Carrier</th><th>Programmes</th><th>Brokers</th>
                  <th>Users</th><th>Status</th><th></th>
                </tr>
              </thead>
              <tbody>
                {items.map(t => {
                  const sb = statusBadge(tenantStatus(t));
                  return (
                    <tr key={t.mga}>
                      <td>
                        <b>{t.name}</b>
                        <div className="sub">
                          {tenantStatus(t) === "invited" ? "invited " : "joined "}
                          {fmtDate(t.created_at)}
                        </div>
                      </td>
                      {/* A brand new carrier shows 0 and 0 — it stays empty
                          until its own admin signs in and builds it. */}
                      <td>{t.programmes ?? 0}</td>
                      <td>{t.brokers ?? 0}</td>
                      <td>{t.users}</td>
                      <td><span className={`badge ${sb.cls}`}><span className="d" />{sb.label}</span></td>
                      <td className="r">
                        <span className="linkish" onClick={() => nav(`/tenants/${encodeURIComponent(t.mga)}`)}>View</span>
                        {/* Only offered while the org actually has an outstanding
                            invite — nothing to resend once everyone has onboarded.
                            While the request is in flight the element carries NO
                            click handler, so it can't be fired twice. */}
                        {(t.pending_invites ?? 0) > 0 && (
                          <>
                            {" · "}
                            {resending === t.mga ? (
                              <span className="linkish mut" aria-disabled="true">Sending…</span>
                            ) : (
                              <span className="linkish" onClick={() => resendInvite(t)}
                                title={`Re-send the onboarding link to ${t.pending_invites} pending ${
                                  t.pending_invites === 1 ? "user" : "users"} of ${t.name}`}>
                                Resend invite
                              </span>
                            )}
                          </>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!loading && total === 0 && (
              <div className="empty">{filtersActive ? "No carriers match the filters." : "No carriers yet."}</div>
            )}
          </div>
          {!loading && total > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={total} onPageChange={setPage} noun="carriers" />
          )}
        </div>

        <div className="note" style={{ marginTop: 14 }}>
          A brand new carrier is empty until its own admin gets started. They add
          the programmes and the brokers, not us.
        </div>
      </div>
    </div>
  );
}
