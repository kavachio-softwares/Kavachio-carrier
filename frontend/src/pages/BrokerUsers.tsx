/**
 * The broker's own Users & Roles.
 *
 * Every company on the platform brings in its own people: Kavachio created the
 * carrier, the carrier created this broker's first admin, and that admin adds
 * its operators here. Nobody staffs anybody else.
 *
 * So the only seat this screen can hand out is OPERATOR. That is not a UI
 * choice — the database says the same thing (an operator may only be created by
 * a broker admin, and a broker admin may not create another one), and a role
 * dropdown here would exist only to be refused.
 */
import { useCallback, useState } from "react";
import {
  getBrokerUsers, inviteBrokerOperator, resendBrokerInvite, removeBrokerUser,
  type BrokerUser,
} from "../api/broker";
import { getUser } from "../auth";
import { fmtDateTime } from "../utils/date";
import { InviteSentModal } from "../components/InviteSentModal";
import { Pagination } from "../components/Pagination";
import { useServerList } from "../hooks/useServerList";

// Ten, as everywhere else in the app.
const PAGE_SIZE = 10;

const ROLE_LABEL: Record<string, string> = {
  broker_admin: "Broker Admin",
  operator: "Broker User",
};

function statusBadge(s: string): { cls: string; label: string } {
  if (s === "active") return { cls: "b-ok", label: "Active" };
  if (s === "invited" || s === "pending") return { cls: "b-warn", label: "Invited" };
  return { cls: "b-mut", label: "Inactive" };
}

export default function BrokerUsers() {
  const me = getUser();
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "warn"; text: string } | null>(null);

  // TRUE server-side paging: the server cuts the page and sends the TEAM'S
  // totals beside it. `total_admins` has to be the team's — "you cannot
  // remove the last admin" is a fact about the organisation, and reading it
  // off ten rows would start offering Remove on the only admin as soon as an
  // eleventh person joined.
  //
  // The fetcher reports its own failures rather than letting the hook swallow
  // them: it empties the list on an error, and an empty list here reads as
  // "nobody works here", which is not the same thing as "we could not ask".
  const fetchPage = useCallback((page: number, pageSize: number) =>
    getBrokerUsers({ page, page_size: pageSize })
      .then(r => { setErr(null); return r; })
      .catch(e => { setErr("Could not load your team."); throw e; }), []);
  const { page, setPage, items, total, extra, pageCount, loading, reload } =
    useServerList<BrokerUser, {
      broker: { id: number; name: string }; total_admins: number;
    }>(fetchPage, "", PAGE_SIZE);
  const brokerName = extra?.broker?.name ?? "your organisation";
  const totalAdmins = extra?.total_admins ?? 0;

  // Invite an operator — a dialog rather than its own page, because there are
  // only two things to fill in and the role is already decided.
  const [inviteOpen, setInviteOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [inviteBusy, setInviteBusy] = useState(false);
  const [inviteErr, setInviteErr] = useState<string | null>(null);
  const [sent, setSent] = useState<{ name: string; email: string } | null>(null);

  const [removeTarget, setRemoveTarget] = useState<BrokerUser | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [removeErr, setRemoveErr] = useState<string | null>(null);

  const canSend = name.trim().length > 0 && email.trim().length > 0;

  function openInvite() {
    setName(""); setEmail(""); setInviteErr(null); setInviteOpen(true);
  }
  function closeInvite() { if (!inviteBusy) setInviteOpen(false); }

  async function sendInvite() {
    if (!canSend) return;
    setInviteBusy(true); setInviteErr(null);
    try {
      await inviteBrokerOperator(name.trim(), email.trim());
      setSent({ name: name.trim(), email: email.trim() });
      setInviteOpen(false);
      reload();
    } catch (e: any) {
      setInviteErr(e?.response?.data?.detail ?? "Could not send the invitation.");
    } finally { setInviteBusy(false); }
  }

  async function resend(u: BrokerUser) {
    setMsg(null);
    try {
      await resendBrokerInvite(u.id);
      setSent({ name: u.full_name || u.email, email: u.email });
    } catch (e: any) {
      setMsg({ kind: "warn", text: e?.response?.data?.detail
        ?? `Couldn't resend the invite to ${u.email}.` });
    }
    reload();
  }

  // Removal rules match the API exactly, so a link is never offered for
  // something the server will refuse.
  function canRemove(u: BrokerUser) {
    if (u.id === me?.id) return false;
    if (u.role === "broker_admin" && totalAdmins <= 1) return false;
    return true;
  }
  function closeRemove() { if (!removeBusy) { setRemoveTarget(null); setRemoveErr(null); } }
  async function confirmRemove() {
    if (!removeTarget) return;
    setRemoveBusy(true); setRemoveErr(null);
    try {
      await removeBrokerUser(removeTarget.id);
      setMsg({ kind: "ok", text: `${removeTarget.email} was removed.` });
      setRemoveTarget(null);
      // Removing the last person on a page takes the page away with them.
      // Step back rather than reload in place, which would leave an empty
      // table under a count that is not zero.
      if (items.length <= 1 && page > 1) setPage(page - 1);
      else reload();
    } catch (e: any) {
      setRemoveErr(e?.response?.data?.detail
        ?? "We couldn't remove this user. Please try again.");
    } finally { setRemoveBusy(false); }
  }

  if (err) return (
    <div className="proto"><div className="view full">
      <div className="page-head"><div className="t"><h2>Users &amp; Roles</h2></div></div>
      <div className="note warn" style={{ maxWidth: 560 }}>{err}</div>
    </div></div>
  );
  // Only the FIRST load blanks the screen. Turning a page keeps the table
  // where it is rather than throwing the reader back to a "Loading…" line.
  if (loading && extra === null) return (
    <div className="proto"><div className="view full">
      <div className="page-head"><div className="t"><h2>Users &amp; Roles</h2></div></div>
      <div className="muted">Loading…</div>
    </div></div>
  );

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Users &amp; Roles</h2>
            {/* <p>
              Your team at {brokerName}. A <b>Broker User</b> sends your
              bordereau files and sorts out the errors they raise. Everything
              else stays with you. All your broker users work on the same
              files, runs and deadlines — what one sends, the others see.
            </p> */}
          </div>
          <div className="actions">
            <button className="btn pri" onClick={openInvite}>＋ Add Broker User</button>
          </div>
        </div>

        {msg && (
          <div className={`note ${msg.kind}`} style={{ marginBottom: 14, maxWidth: 560 }}>
            {msg.text}
          </div>
        )}

        <div className="card">
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th><th>Role</th>
                  <th>Status</th><th>Last sign-in</th>
                  <th style={{ textAlign: "right" }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {items.map(u => {
                  const sb = statusBadge(u.status);
                  const invited = u.status === "invited" || u.status === "pending";
                  return (
                    <tr key={u.id}>
                      <td>
                        <b>{u.full_name}</b>
                        <div className="sub">{u.email}</div>
                      </td>
                      <td>
                        <span className={`badge ${u.role === "broker_admin" ? "b-info" : "b-mut"}`}>
                          <span className="d" />{ROLE_LABEL[u.role] ?? u.role}
                        </span>
                      </td>
                      <td><span className={`badge ${sb.cls}`}><span className="d" />{sb.label}</span></td>
                      <td className="muted">{fmtDateTime(u.last_login_at)}</td>
                      <td className="r">
                        {/* BUTTONS, not linked words joined by a middot, and
                            flushed right — the same treatment as the carrier's
                            Users & Roles, which is the same screen for the
                            other seat. The gap replaces the separator, so the
                            dangling-dot case it guarded against cannot arise. */}
                        <div className="rowacts"
                             style={{ marginTop: 0, justifyContent: "flex-end" }}>
                          {invited && (
                            <button type="button" className="btn sm"
                                    onClick={() => resend(u)}>
                              Resend
                            </button>
                          )}
                          {/* Removal is offered only where the server would allow
                              it. When it wouldn't — your own account, or the only
                              admin — the action is simply absent rather than drawn
                              greyed out: a disabled "Remove" on your own row reads
                              as something you might be able to do, and there is
                              nothing on this screen that could make it enabled. */}
                          {canRemove(u) && (
                            <button type="button" className="btn sm danger"
                              title="Remove this user"
                              onClick={() => { setRemoveErr(null); setRemoveTarget(u); }}>
                              Remove
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {total === 0 && <div className="empty">No one here yet.</div>}
          </div>
          {total > 0 && (
            <Pagination page={page} pageCount={pageCount} pageSize={PAGE_SIZE}
              totalItems={total} onPageChange={setPage} noun="people" />
          )}
        </div>

        {/* <div className="note" style={{ marginTop: 16, maxWidth: 720 }}>
          A broker user only sees what they need to run a file: what is due, the
          run screen, and the errors to sort out. They never see a contract, a setup
          or the carrier's business — so a new starter can be useful on day one
          without being able to change anything that was agreed.
        </div> */}
      </div>

      {/* Invite an operator */}
      {inviteOpen && (
        <div className="proto-modal-overlay" onClick={closeInvite}>
          <div className="proto-modal" onClick={e => e.stopPropagation()}>
            <div className="m-h">
              <h3>Add a broker user to {brokerName}</h3>
              <button className="x" onClick={closeInvite} aria-label="Close">×</button>
            </div>
            <div className="m-b">
              <div className="field">
                <label>Full name</label>
                <input value={name} autoFocus placeholder="e.g. Ana Ferreira"
                  onChange={e => setName(e.target.value)} />
              </div>
              <div className="field">
                <label>Email</label>
                <input type="email" value={email} placeholder="name@company.com"
                  onChange={e => setEmail(e.target.value)} />
                <div className="hint">The sign-up link is sent here. It lasts 7 days.</div>
              </div>
              <div className="note" style={{ marginBottom: 0 }}>
                They join as a <b>Broker User</b>: three pages — what is due, the run
                screen, and the errors to sort out. No contracts, no setup, and
                nothing of the carrier's. Another admin can only be added by the
                carrier, the same way you were.
              </div>
              {inviteErr && (
                <div style={{ marginTop: 10, color: "var(--p-crit)" }}>{inviteErr}</div>
              )}
            </div>
            <div className="m-f">
              <button className="btn" onClick={closeInvite} disabled={inviteBusy}>Cancel</button>
              <button className="btn pri" onClick={sendInvite} disabled={inviteBusy || !canSend}
                title={canSend ? undefined : "Enter a name and email first"}>
                {inviteBusy ? "Sending…" : "Send invitation"}
              </button>
            </div>
          </div>
        </div>
      )}

      {sent && (
        <InviteSentModal
          title="Invitation sent"
          message={`${sent.name} can now set a password and sign in.`}
          email={sent.email}
          note="They arrive as a Broker User. The link expires in 7 days."
          onDone={() => setSent(null)}
        />
      )}

      {/* Remove — same dialog shape as the carrier's Users screen. */}
      {removeTarget && (
        <div className="proto-modal-overlay" onClick={closeRemove}>
          <div className="proto-modal" onClick={e => e.stopPropagation()}>
            <div className="m-h">
              <h3>Remove User</h3>
              <button className="x" onClick={closeRemove} aria-label="Close">×</button>
            </div>
            <div className="m-b">
              Remove <b>{removeTarget.full_name || removeTarget.email}</b> from {brokerName}?
              They lose access straight away. Everything they did — runs, uploads —
              keeps their name on it, so your records stay complete.
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
