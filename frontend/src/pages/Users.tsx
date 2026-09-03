import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { currentMga, getUser, normalizeRole } from "../auth";
import { fmtDateTime } from "../utils/date";
import { ListFilterBar } from "../components/ListFilterBar";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { InviteSentModal } from "../components/InviteSentModal";

type U = {
  id: number; email: string; full_name: string;
  role: string; status: string; mga: string;
  last_login_at?: string | null;
  /** Broker people appear here too now — this says which organisation. */
  broker_party_id?: number | null;
  org_name?: string | null;
  org_kind?: "carrier" | "broker";
};

// The API normalizes every stored/legacy role string down to the four-role
// vocabulary (kavachio_admin rows are filtered out server-side, so a tenant
// never sees the platform account in its own list).
const ROLE_LABEL: Record<string, string> = {
  carrier_admin: "Carrier Admin",
  broker_admin: "Broker Admin",
  operator: "Operator",
};

// What each seat can actually do — the reason anyone reads this table.
const ROLE_CAN_DO: Record<string, string> = {
  carrier_admin: "Everything you can, including approving contracts",
  broker_admin: "Their contracts and file setups; adds their own staff",
  operator: "Processing and exceptions only",
};

// Single source of truth for a user's displayed status bucket — used by both
// the Status filter and the badge, so they can never drift out of sync.
type StatusKey = "active" | "invited" | "inactive";
const STATUS_LABEL: Record<StatusKey, string> = { active: "Active", invited: "Invited", inactive: "Inactive" };
function statusKey(s: string): StatusKey {
  if (s === "active") return "active";
  if (s === "pending" || s === "invited") return "invited";
  return "inactive";
}
function statusBadge(s: string): { cls: string; label: string } {
  const k = statusKey(s);
  if (k === "active") return { cls: "b-ok", label: "Active" };
  if (k === "invited") return { cls: "b-warn", label: "Invited" };
  return { cls: "b-mut", label: "Inactive" };
}

const PAGE_SIZE = 10;

export default function Users() {
  const mga = currentMga();
  const me = getUser();
  const nav = useNavigate();
  const [msg, setMsg] = useState<{ kind: "ok" | "warn"; text: string } | null>(null);
  // A successful resend is confirmed with the SAME popup as a first-time
  // invite (Invite User), not a small inline banner.
  const [resent, setResent] = useState<{ name: string; email: string } | null>(null);
  const [q, setQ] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const dq = useDebouncedValue(q, 300);
  // reset-password confirm modal
  const [resetTarget, setResetTarget] = useState<U | null>(null);
  const [resetBusy, setResetBusy] = useState(false);
  const [resetErr, setResetErr] = useState<string | null>(null);
  const [resetSent, setResetSent] = useState(false);

  // TRUE server-side pagination: the backend filters (q/role/status) + pages;
  // we send the current filters and receive just this page + the matching
  // total, PLUS the tenant-wide admin count (total_admins) — "can't remove the
  // last admin" needs the true total, not just however many are on this page.
  const filterKey = `${dq}|${roleFilter}|${statusFilter}`;
  const { page, setPage, items, total, extra, pageCount, reload } = useServerList<
    U, { total_admins: number }
  >(
    (page, pageSize) =>
      api.get<{ items: U[]; total: number; total_admins: number }>("/users", {
        params: {
          mga, page, page_size: pageSize,
          q: dq || undefined, role: roleFilter || undefined, status: statusFilter || undefined,
        },
      }).then(r => r.data),
    filterKey,
    PAGE_SIZE,
  );
  const pageRows = items;
  const totalItems = total;
  const adminCount = extra?.total_admins ?? 0;
  function canRemove(u: U) {
    if (u.id === me?.id) return false;          // can't remove yourself
    if (normalizeRole(u.role) === "operator") return false;  // the broker's seat, not yours
    if (normalizeRole(u.role) === "carrier_admin" && adminCount <= 1) return false; // can't remove last admin
    return true;
  }

  const filtersActive = q !== "" || roleFilter !== "" || statusFilter !== "";
  function clearFilters() { setQ(""); setRoleFilter(""); setStatusFilter(""); }

  // Removal is confirmed in an in-app dialog (matching Reset Password below)
  // rather than a native confirm(), and the blocked case explains WHY instead
  // of firing an alert from a link that already looks disabled.
  const [removeTarget, setRemoveTarget] = useState<U | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeErr, setRemoveErr] = useState<string | null>(null);

  function askRemove(u: U) {
    if (!canRemove(u)) return;   // link is visibly muted; nothing to explain in a popup
    setRemoveErr(null);
    setRemoveTarget(u);
  }
  function closeRemove() { if (!removeBusy) { setRemoveTarget(null); setRemoveErr(null); } }
  async function confirmRemove() {
    if (!removeTarget) return;
    setRemoveBusy(true); setRemoveErr(null);
    try {
      const { data } = await api.delete<{ suspended?: boolean; message?: string | null }>(
        `/users/${removeTarget.id}`);
      // Someone other records name is suspended rather than deleted, so the
      // trail of who did what stays readable. Say which happened.
      setMsg({ kind: "ok", text: data?.message ?? `${removeTarget.email} was removed.` });
      setRemoveTarget(null);
      reload();
    } catch (e: any) {
      // Previously this call had no catch at all, so a failed delete looked
      // like nothing happened. Keep the dialog open and say what went wrong.
      setRemoveErr(e?.response?.data?.detail
        ?? "We couldn't remove this user. Please try again.");
    } finally { setRemoveBusy(false); }
  }

  // Resend invite → re-issues a set-password email to an invited user.
  // Success is confirmed with the invite popup (same one as Invite User) so
  // both ways of sending an invite look identical; only a failure falls back
  // to an inline banner.
  async function resend(u: U) {
    setMsg(null);
    try {
      await api.post(`/users/${u.id}/resend-invite`);
      setResent({ name: u.full_name || u.email, email: u.email });
    } catch (e: any) {
      setMsg({ kind: "warn", text: e?.response?.data?.detail
        ?? `Couldn't resend the invite to ${u.email} — please try again.` });
    }
    reload();
  }

  // Reset password → opens an on-screen confirm modal (below).
  function resetPassword(u: U) {
    setResetTarget(u); setResetSent(false); setResetErr(null);
  }
  async function confirmReset() {
    if (!resetTarget) return;
    setResetBusy(true); setResetErr(null);
    try {
      await api.post("/auth/forgot", { email: resetTarget.email });
      setResetSent(true);
    } catch {
      setResetErr("Couldn't send the reset link — please try again.");
    } finally { setResetBusy(false); }
  }
  function closeReset() { if (!resetBusy) setResetTarget(null); }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Users &amp; Roles</h2>
            <p>
              Everyone who signs in on your side of the platform — your own team,
              and the admins at the brokers who send you files. Operators are added
              by their own broker, so they appear here but are not yours to change.
            </p>
          </div>
          <div className="actions">
            <button className="btn pri" onClick={() => nav("/users/new")}>＋ Invite User</button>
          </div>
        </div>

        {msg && (
          <div className={`note ${msg.kind}`} style={{ marginBottom: 14, maxWidth: 560 }}>{msg.text}</div>
        )}

        {resent && (
          <InviteSentModal
            title="Invite re-sent"
            message={`A new sign-up link is on its way to ${resent.name}.`}
            email={resent.email}
            note="The earlier link no longer works — they'll set a password with this one."
            onDone={() => setResent(null)}
          />
        )}

        <div className="card">
          <ListFilterBar
            search={{ value: q, onChange: setQ, placeholder: "Search name or email…" }}
            selects={[
              {
                key: "role", ariaLabel: "Filter by role", value: roleFilter, onChange: setRoleFilter,
                options: [{ value: "", label: "All Roles" },
                  ...Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))],
              },
              {
                key: "status", ariaLabel: "Filter by status", value: statusFilter, onChange: setStatusFilter,
                options: [{ value: "", label: "All Statuses" },
                  ...(Object.entries(STATUS_LABEL) as [StatusKey, string][])
                    .map(([value, label]) => ({ value, label }))],
              },
            ]}
            onClear={clearFilters}
            active={filtersActive}
          />
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Name</th><th>Organisation</th><th>Role</th><th>Can do</th>
                  <th>Status</th><th>Last Sign-In</th><th></th></tr>
              </thead>
              <tbody>
                {pageRows.map(u => {
                  const sb = statusBadge(u.status);
                  const role = normalizeRole(u.role);
                  const isAdminRow = role === "carrier_admin";
                  const invited = u.status === "pending" || u.status === "invited";
                  return (
                    <tr key={u.id}>
                      <td>
                        <b>{u.full_name}</b>
                        <div className="sub">{u.email}</div>
                      </td>
                      {/* Which organisation this person is an admin OF. Without
                          it "Broker Admin" says the seat but not the company. */}
                      <td>
                        {u.org_name ?? "—"}
                        <div className="sub">
                          {u.org_kind === "broker" ? "Broker" : "Your organisation"}
                        </div>
                      </td>
                      <td>
                        <span className={`badge ${isAdminRow ? "b-info" : "b-mut"}`}>
                          <span className="d" />{ROLE_LABEL[role] ?? role}
                        </span>
                      </td>
                      <td className="l">{ROLE_CAN_DO[role] ?? "—"}</td>
                      <td><span className={`badge ${sb.cls}`}><span className="d" />{sb.label}</span></td>
                      <td className="muted">{fmtDateTime(u.last_login_at)}</td>
                      <td className="r">
                        {invited ? (
                          <span className="linkish" onClick={() => resend(u)}>
                            Resend Invite
                          </span>
                        ) : (
                          <span className="linkish" onClick={() => resetPassword(u)}>
                            Reset Password
                          </span>
                        )}
                        {" · "}
                        {/* When removal isn't allowed the element carries NO click
                            handler at all — it's inert, not a link that silently
                            does nothing — and aria-disabled drives the styling
                            (grey, not-allowed cursor, no hover underline). The
                            title says which rule applies. */}
                        {canRemove(u) ? (
                          <span className="linkish" title="Remove this user"
                            onClick={() => askRemove(u)}>
                            Remove
                          </span>
                        ) : (
                          <span className="linkish mut" aria-disabled="true"
                            title={u.id === me?.id
                              ? "You can't remove your own account."
                              : normalizeRole(u.role) === "operator"
                                ? "Operators belong to the broker. Their own admin adds and removes them."
                                : "This is the only admin — add another before removing this one."}>
                            Remove
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {totalItems === 0 && !filtersActive && <div className="empty">No users yet.</div>}
            {totalItems === 0 && filtersActive && <div className="empty">No users match the filters.</div>}
          </div>
          {totalItems > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={totalItems} onPageChange={setPage} noun="users" />
          )}
        </div>

        <div className="note" style={{ marginTop: 14, maxWidth: 560 }}>
          Bringing a broker on board? Invite their admin above — the broker
          organisation is created with the invitation. Put them on a programme
          from Programmes; until then they cannot produce.
        </div>
      </div>

      {resetTarget && (
        <div className="proto-modal-overlay" onClick={closeReset}>
          <div className="proto-modal" onClick={e => e.stopPropagation()}>
            {resetSent ? (
              <>
                <div className="m-h">
                  <h3>Reset Link Sent</h3>
                  <button className="x" onClick={closeReset} aria-label="Close">×</button>
                </div>
                <div className="m-b">
                  A password reset link has been emailed to <b>{resetTarget.email}</b>.
                  It expires in 30 minutes.
                </div>
                <div className="m-f">
                  <button className="btn pri" onClick={closeReset}>Done</button>
                </div>
              </>
            ) : (
              <>
                <div className="m-h">
                  <h3>Reset Password</h3>
                  <button className="x" onClick={closeReset} aria-label="Close">×</button>
                </div>
                <div className="m-b">
                  Send a password reset link to <b>{resetTarget.email}</b>? They'll get an email
                  with a 30-minute link to set a new password.
                  {resetErr && (
                    <div style={{ marginTop: 10, color: "var(--p-crit)" }}>{resetErr}</div>
                  )}
                </div>
                <div className="m-f">
                  <button className="btn" onClick={closeReset} disabled={resetBusy}>Cancel</button>
                  <button className="btn pri" onClick={confirmReset} disabled={resetBusy}>
                    Send Reset Link
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* Remove user — same in-app dialog pattern as Reset Password above, so
          the two destructive actions on this screen look like one product. */}
      {removeTarget && (
        <div className="proto-modal-overlay" onClick={closeRemove}>
          <div className="proto-modal" onClick={e => e.stopPropagation()}>
            <div className="m-h">
              <h3>Remove User</h3>
              <button className="x" onClick={closeRemove} aria-label="Close">×</button>
            </div>
            <div className="m-b">
              Remove <b>{removeTarget.full_name || removeTarget.email}</b> ({removeTarget.email})
              from your organization? They will lose access immediately.
              {removeErr && (
                <div style={{ marginTop: 10, color: "var(--p-crit)" }}>{removeErr}</div>
              )}
            </div>
            <div className="m-f">
              <button className="btn" onClick={closeRemove} disabled={removeBusy}>Cancel</button>
              <button className="btn pri" onClick={confirmRemove} disabled={removeBusy}>
                {removeBusy ? "Removing…" : "Remove User"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
