/**
 * The "Reinsurer access" tab on a treaty's record.
 *
 * Reinsurers are not on Kavachio, so a reinsurer's people cannot log in to see
 * their line. The carrier sends each of them a PRIVATE, READ-ONLY link instead:
 * it opens only after the one-time code emailed to the same address is typed,
 * and it shows that reinsurer its own share of each layer — never another
 * reinsurer's name or share.
 *
 *   Share       pick contacts at the reinsurers on this treaty; each gets a link
 *   Who has it  every link, its state, resend (a new URL — the old one dies)
 *               and withdraw
 *   History     every code, unlock, view and download, in order
 *
 * Contacts are the party's own (Parties → the reinsurer → Contacts). A link is
 * tied to one contact: delete the contact and their link stops opening.
 */
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Ban, CheckCircle2, Eye, KeyRound, Lock, Mail, MessageSquare, RefreshCw, Send,
  ShieldCheck, Download, XCircle,
} from "lucide-react";
import { InfoTip } from "../InfoTip";
import { fieldErrors } from "../../api/contractRecord";
import {
  getReinsurerAccess, resendReinsurerLink, revokeReinsurerLink,
  shareWithReinsurers, type AccessEvent, type AccessLink, type ReinsurerAccess,
} from "../../api/treaty";
import { fmtDate, fmtStamp } from "../../utils/date";

const STATUS: Record<AccessLink["status"], { label: string; cls: string }> = {
  active: { label: "Active", cls: "b-ok" },
  revoked: { label: "Withdrawn", cls: "b-mut" },
  expired: { label: "Expired", cls: "b-mut" },
  closed: { label: "Closed", cls: "b-mut" },
};

const EVENT: Record<AccessEvent["type"], { label: string; icon: React.ElementType }> = {
  sent: { label: "Link sent", icon: Send },
  code_sent: { label: "Asked for a new code", icon: Mail },
  code_failed: { label: "Entered a wrong code", icon: KeyRound },
  locked: { label: "Locked out after too many wrong codes", icon: Lock },
  unlocked: { label: "Entered the code", icon: ShieldCheck },
  viewed: { label: "Viewed the treaty", icon: Eye },
  downloaded: { label: "Downloaded a document", icon: Download },
  revoked: { label: "Link withdrawn", icon: Ban },
  refused: { label: "Tried a closed link", icon: XCircle },
  approved: { label: "Approved the treaty", icon: CheckCircle2 },
  changes_requested: { label: "Asked for a change", icon: MessageSquare },
};

const APPROVAL: Record<string, { label: string; cls: string }> = {
  approved: { label: "Approved", cls: "b-ok" },
  changes_requested: { label: "Asked for a change", cls: "b-warn" },
  stale: { label: "Answered an earlier version", cls: "b-warn" },
  pending: { label: "Waiting", cls: "b-mut" },
};

export function ReinsurerSharing({ contractId }: { contractId: number }) {
  const [data, setData] = useState<ReinsurerAccess | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(() => {
    getReinsurerAccess(contractId).then(setData)
      .catch(e => setErr(fieldErrors(e).message));
  }, [contractId]);
  useEffect(load, [load]);

  async function act(label: string, fn: () => Promise<ReinsurerAccess & {
    sent?: { name: string; emailed: boolean }[]; emailed?: boolean }>,
                     ok: (r: { sent?: { name: string; emailed: boolean }[];
                               emailed?: boolean }) => string) {
    setBusy(label); setErr(""); setNote("");
    try {
      const r = await fn();
      setData(r);
      setNote(ok(r));
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  if (!data) {
    return <div className="card pad">{err ? <div className="note warn">{err}</div>
      : <div className="sub">Loading…</div>}</div>;
  }

  const activeFor = new Map(data.links.filter(l => l.status === "active")
    .map(l => [l.contact_id, l]));
  const events = showAll ? data.events : data.events.slice(0, 15);

  return (
    <div>
      {note && <div className="note ok" style={{ marginBottom: 12 }}>{note}</div>}
      {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}

      {/* ── approvals ── */}
      {data.approvals.total > 0 && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-h">
            <CheckCircle2 size={16} className="ci" />
            <h3>Reinsurers&apos; approval</h3>
            <span className={`badge ${data.approvals.all_approved ? "b-ok" : "b-mut"}`}>
              <span className="d" />
              {data.approvals.approved} of {data.approvals.total} approved
            </span>
            <InfoTip text={"Each reinsurer approves the treaty on its private link. "
              + "When every one has approved a draft, its terms are agreed. If you "
              + "change the treaty, earlier approvals no longer count and the "
              + "reinsurers are asked again."} />
          </div>
          <div className="tbl-wrap">
            <table>
              <tbody>
                {data.approvals.parties.map(p => (
                  <tr key={p.party_id}>
                    <td style={{ textAlign: "left" }}>
                      <b>{p.party_name}</b>
                      {p.is_lead && <span className="sub"> · lead</span>}
                    </td>
                    <td style={{ textAlign: "left" }}>
                      <span className={`badge ${APPROVAL[p.status].cls}`}>
                        <span className="d" />{APPROVAL[p.status].label}
                      </span>
                      {p.status === "changes_requested" && p.note && (
                        <div className="sub" style={{ marginTop: 4, color: "var(--p-warn-ink)" }}>
                          &ldquo;{p.note}&rdquo;
                        </div>
                      )}
                    </td>
                    <td className="sub" style={{ textAlign: "left" }}>
                      {p.at
                        ? `${p.signer_name ?? ""}${p.signer_title ? `, ${p.signer_title}` : ""} · ${fmtStamp(p.at)}`
                        : "Not answered yet"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {data.approvals.parties.some(p => p.status === "pending" || p.status === "stale") && (
            <div className="sub" style={{ padding: "10px 20px 14px" }}>
              A reinsurer answers on its private link. Send one below to anyone
              who has not got it yet.
            </div>
          )}
        </div>
      )}

      {/* ── share ── */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-h">
          <ShieldCheck size={16} className="ci" />
          <h3>Share with a reinsurer</h3>
          <InfoTip text={"Reinsurers are not on Kavachio. Each person you pick "
            + "is emailed a private link and a one-time code. The link is "
            + "read-only, shows only their own organisation's lines, and asks "
            + "for the code again in every new browser session."} />
        </div>
        <div style={{ padding: "14px 20px" }}>
          {!data.can_share ? (
            <div className="note">{data.why_not}</div>
          ) : data.contacts.length === 0 ? (
            <div className="empty" style={{ padding: 16 }}>
              No reinsurer is on this treaty's panel yet.
            </div>
          ) : (
            <>
              {data.contacts.map(g => (
                <div key={g.party_id} style={{ marginBottom: 14 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8,
                                marginBottom: 6 }}>
                    <b style={{ fontSize: 13 }}>{g.party_name}</b>
                    {g.is_lead_reinsurer && (
                      <span className="badge b-ok"><span className="d" />Lead reinsurer</span>
                    )}
                  </div>
                  {g.contacts.length === 0 ? (
                    <div className="sub">
                      No contacts yet.{" "}
                      <Link className="linkish" to={`/parties/${g.party_id}`}>
                        Add one on {g.party_name}'s page
                      </Link>{" "}— they need a name and an email.
                    </div>
                  ) : (
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                      {g.contacts.map(ct => {
                        const has = activeFor.get(ct.id);
                        const on = picked.includes(ct.id);
                        return (
                          <label key={ct.id}
                                 className={`badge ${on ? "b-ok" : ""}`}
                                 title={ct.email ? undefined : "No email address"}
                                 style={{ border: "1px solid var(--p-border-2)",
                                          padding: "6px 11px", fontSize: 12.5,
                                          cursor: ct.email ? "pointer" : "not-allowed",
                                          opacity: ct.email ? 1 : .55,
                                          background: on ? undefined : "var(--p-surface)" }}>
                            <input type="checkbox" checked={on} disabled={!ct.email}
                                   style={{ margin: 0 }}
                                   onChange={e => setPicked(e.target.checked
                                     ? [...picked, ct.id]
                                     : picked.filter(x => x !== ct.id))} />
                            {ct.name}
                            <span className="sub">{ct.email ?? "no email"}</span>
                            {has && <span className="sub">· has a link</span>}
                          </label>
                        );
                      })}
                    </div>
                  )}
                </div>
              ))}
              {picked.some(p => activeFor.has(p)) && (
                <div className="sub" style={{ marginBottom: 8 }}>
                  Somebody you picked already has a link. They get a new one and
                  the old one stops working.
                </div>
              )}
              <div className="rowacts">
                <button className="btn pri" type="button"
                        disabled={!picked.length || !!busy}
                        onClick={() => act("share",
                          () => shareWithReinsurers(contractId, picked),
                          r => {
                            setPicked([]);
                            const failed = (r.sent ?? []).filter(x => !x.emailed);
                            return failed.length
                              ? `Links created, but the email to ${failed.map(f => f.name)
                                  .join(", ")} could not be sent. Use Resend below.`
                              : `Sent to ${(r.sent ?? []).map(x => x.name).join(", ")}.`
                                + " Each has their own link and code.";
                          })}>
                  <Send size={13} /> {busy === "share" ? "Sending…"
                    : `Send ${picked.length || ""} private link${picked.length === 1 ? "" : "s"}`}
                </button>
                <span className="sub">
                  They can see the structure, their own shares and the signed
                  wording. The wording names the lead reinsurer, as a signed
                  treaty does.
                </span>
              </div>
            </>
          )}
        </div>
      </div>

      {/* ── who has it ── */}
      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-h">
          <h3>Who it has been shared with</h3>
          <span className="sub">{data.links.length} link{data.links.length === 1 ? "" : "s"}</span>
        </div>
        {data.links.length === 0 ? (
          <div className="empty">Not shared with anybody yet.</div>
        ) : (
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr>
                  <th>Person</th><th>Status</th><th>Sent</th>
                  <th>Last viewed</th><th>Works until</th><th />
                </tr>
              </thead>
              <tbody>
                {data.links.map(l => (
                  <tr key={l.id}>
                    <td>
                      <b>{l.name}</b>
                      <div className="sub">{l.email} · {l.party_name}</div>
                    </td>
                    <td>
                      <span className={`badge ${STATUS[l.status].cls}`}>
                        <span className="d" />{STATUS[l.status].label}
                      </span>
                      {l.locked && l.status === "active" && (
                        <div className="sub" style={{ color: "var(--p-warn-ink)" }}>
                          Locked for a few minutes — too many wrong codes
                        </div>
                      )}
                      {l.status !== "active" && l.status_title && (
                        <div className="sub">{l.status_title}</div>
                      )}
                    </td>
                    <td className="sub">{fmtStamp(l.sent_at)}</td>
                    <td className="sub">
                      {l.last_viewed_at ? fmtStamp(l.last_viewed_at) : "Not opened yet"}
                    </td>
                    <td className="sub">{fmtDate(l.expires_on)}</td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {l.status !== "closed" && (
                        <button className="btn sm" type="button" disabled={!!busy}
                                title="Send a new link and code. The old link stops working."
                                onClick={() => act(`resend-${l.id}`,
                                  () => resendReinsurerLink(contractId, l.id),
                                  r => r.emailed === false
                                    ? `A new link was made for ${l.name}, but the email could not be sent.`
                                    : `A new link and code went to ${l.email}. The old link no longer works.`)}>
                          <RefreshCw size={12} /> Resend
                        </button>
                      )}{" "}
                      {l.status === "active" && (
                        <button className="btn sm danger" type="button" disabled={!!busy}
                                onClick={() => act(`revoke-${l.id}`,
                                  () => revokeReinsurerLink(contractId, l.id),
                                  () => `${l.name}'s link was withdrawn. It now says so when opened.`)}>
                          <Ban size={12} /> Withdraw
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="sub" style={{ padding: "10px 20px 14px" }}>
          A link also stops working by itself when the treaty is terminated or
          renewed, when the contact is removed, or when their organisation no
          longer holds a share.
        </div>
      </div>

      {/* ── history ── */}
      <div className="card">
        <div className="card-h">
          <h3>Access history</h3>
          <InfoTip text="Every link sent, every code, every unlock, view and download — with the time and the address it came from." />
        </div>
        <div style={{ padding: "6px 20px 12px" }}>
          {data.events.length === 0 ? (
            <div className="empty" style={{ padding: 16 }}>Nothing yet.</div>
          ) : events.map(e => {
            const ev = EVENT[e.type] ?? { label: e.type, icon: Eye };
            const Icon = ev.icon;
            return (
              <div className="evt" key={e.id}>
                <span className="t">{fmtStamp(e.at)}</span>
                <Icon size={14} style={{ color: "var(--p-muted)", flex: "0 0 auto",
                                         marginTop: 2 }} />
                <span style={{ flex: 1 }}>
                  <b>{ev.label}</b>{" "}
                  <span className="muted">
                    {e.type === "sent" || e.type === "revoked"
                      ? `— ${e.who ?? ""}${e.party_name ? ` at ${e.party_name}` : ""}`
                        + (e.actor ? `, by ${e.actor}` : "")
                      : `— ${e.who ?? ""}${e.party_name ? `, ${e.party_name}` : ""}`}
                    {e.type === "downloaded" && e.detail?.filename
                      ? ` · ${String(e.detail.filename)}` : ""}
                  </span>
                  {e.ip && <div className="sub">from {e.ip}</div>}
                </span>
              </div>
            );
          })}
          {data.events.length > 15 && (
            <button className="btn sm ghost" type="button"
                    onClick={() => setShowAll(v => !v)}>
              {showAll ? "Show fewer" : `Show all ${data.events.length}`}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
