/**
 * One reinsurer — opened from the directory, or from a treaty's panel.
 *
 * Rendered by PartyDetail for a party of type `reinsurer`. A reinsurer is not
 * a carrier and has no seat here, so the carrier-shaped page (programmes,
 * bordereau setups) has nothing to say about it. What matters instead:
 *
 *   Who they are   name, rating, country
 *   Their people   contacts — a contact's email is where a treaty's private
 *                  read-only link goes, and deleting a contact closes theirs
 *   Their lines    every treaty they are on, and their share of each layer
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ShieldCheck, Trash2, UserPlus } from "lucide-react";
import { api } from "../api/client";
import { isTenantAdmin } from "../auth";
import { InfoTip } from "../components/InfoTip";
import CountryOptions from "../components/CountryOptions";
import { fieldErrors } from "../api/contractRecord";
import {
  addPartyContact, getReinsurerTreaties, listPartyContacts, removePartyContact,
  type ReinsurerContact, type ReinsurerTreaty,
} from "../api/treaty";
import { fmtDate } from "../utils/date";

export type ReinsurerParty = {
  id: number; scope: string; party_type: string; legal_name: string;
  am_best_rating?: string | null; domicile_country?: string | null;
  notes?: string | null; is_active: boolean;
};

const AM_BEST = ["A++ (Superior)", "A+ (Superior)", "A (Excellent)",
                 "A- (Excellent)", "B++ (Good)", "B+ (Good)"];

const LIFE: Record<string, string> = {
  draft: "b-mut", pending: "b-warn", agreed: "b-ok", signed: "b-ok",
  active: "b-ok", expired: "b-mut", terminated: "b-crit", superseded: "b-mut",
};

export default function ReinsurerDetail({ party }: { party: ReinsurerParty }) {
  const nav = useNavigate();
  const isAdmin = isTenantAdmin();
  const readOnly = party.scope === "global" || !isAdmin;
  const [p, setP] = useState(party);
  const [contacts, setContacts] = useState<ReinsurerContact[] | null>(null);
  const [treaties, setTreaties] = useState<ReinsurerTreaty[] | null>(null);
  const [cName, setCName] = useState("");
  const [cTitle, setCTitle] = useState("");
  const [cEmail, setCEmail] = useState("");
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    listPartyContacts(party.id).then(setContacts).catch(() => setContacts([]));
    getReinsurerTreaties(party.id).then(setTreaties).catch(() => setTreaties([]));
  }, [party.id]);

  async function save() {
    setBusy(true); setErr(""); setMsg("");
    try {
      const { data } = await api.put(`/parties/${p.id}`, {
        party_type: "reinsurer", legal_name: p.legal_name,
        am_best_rating: p.am_best_rating || null,
        domicile_country: p.domicile_country || null,
        is_active: p.is_active, notes: p.notes, scope: p.scope,
      });
      setP(data); setMsg("Saved.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  async function addContact() {
    setBusy(true); setErr(""); setMsg("");
    try {
      const c = await addPartyContact(p.id, { full_name: cName.trim(),
        email: cEmail.trim(), title: cTitle.trim() || null });
      setContacts(list => [...(list ?? []), c]);
      setCName(""); setCTitle(""); setCEmail("");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  async function removeContact(c: ReinsurerContact) {
    setBusy(true); setErr(""); setMsg("");
    try {
      await removePartyContact(p.id, c.id);
      setContacts(list => (list ?? []).filter(x => x.id !== c.id));
      setMsg(`${c.full_name} was removed. Any treaty link sent to them has stopped working.`);
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  const emailOk = /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(cEmail.trim());

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2 style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {p.legal_name}
              <span className="badge b-info"><span className="d" />Reinsurer</span>
            </h2>
            <p>Not on Kavachio. Your record of them, and who to send treaty links to.</p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/parties?type=reinsurer")}>
              ← Reinsurers
            </button>
            {!readOnly && (
              <button className="btn pri" onClick={save} disabled={busy}>Save changes</button>
            )}
          </div>
        </div>

        {msg && <div className="note ok" style={{ marginBottom: 14 }}>{msg}</div>}
        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}

        <div className="grid g-2" style={{ alignItems: "start" }}>
          <div className="card pad">
            <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Who they are</h3>
            <div className="field">
              <label>Legal name</label>
              <input value={p.legal_name} disabled={readOnly}
                     onChange={e => setP({ ...p, legal_name: e.target.value })} />
            </div>
            <div className="grid g-2" style={{ gap: 14 }}>
              <div className="field">
                <label>AM Best rating</label>
                <select value={p.am_best_rating ?? ""} disabled={readOnly}
                        onChange={e => setP({ ...p, am_best_rating: e.target.value })}>
                  <option value="">—</option>
                  {AM_BEST.map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
              <div className="field">
                <label>Country</label>
                <select value={p.domicile_country ?? ""} disabled={readOnly}
                        onChange={e => setP({ ...p, domicile_country: e.target.value })}>
                  <option value="">—</option>
                  <CountryOptions />
                </select>
              </div>
            </div>
            <label className="toggle"
              style={{ opacity: readOnly ? 0.6 : 1, cursor: readOnly ? "default" : "pointer" }}
              onClick={() => !readOnly && setP({ ...p, is_active: !p.is_active })}>
              <span className={`sw2${p.is_active ? " on" : ""}`} />
              {p.is_active ? "Active" : "Inactive — not offered on new panels"}
            </label>
          </div>

          <div className="card">
            <div className="card-h">
              <h3>Contacts</h3>
              <InfoTip text={"A contact's email is where a treaty's private, read-only "
                + "link is sent. Removing a contact closes any link they have."} />
            </div>
            <div className="tbl-wrap">
              <table>
                <tbody>
                  {contacts?.length === 0 && (
                    <tr><td className="sub">No contacts yet.</td></tr>
                  )}
                  {(contacts ?? []).map(c => (
                    <tr key={c.id}>
                      <td>
                        <b>{c.full_name}</b>
                        <div className="sub">
                          {[c.title, c.email ?? "no email"].filter(Boolean).join(" · ")}
                        </div>
                      </td>
                      <td className="r" style={{ width: 50 }}>
                        {!readOnly && (
                          <button className="btn sm ghost" type="button" disabled={busy}
                                  aria-label={`Remove ${c.full_name}`}
                                  onClick={() => removeContact(c)}>
                            <Trash2 size={13} />
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!readOnly && (
              <div style={{ padding: "12px 20px 16px" }}>
                <div className="grid g-3" style={{ gap: 10 }}>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <input placeholder="Name" value={cName}
                           onChange={e => setCName(e.target.value)} />
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <input placeholder="Title (optional)" value={cTitle}
                           onChange={e => setCTitle(e.target.value)} />
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <input type="email" placeholder="Email" value={cEmail}
                           onChange={e => setCEmail(e.target.value)} />
                  </div>
                </div>
                <div className="rowacts">
                  <button className="btn sm" type="button"
                          disabled={busy || !cName.trim() || !emailOk}
                          onClick={addContact}>
                    <UserPlus size={13} /> Add contact
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="card" style={{ marginTop: 18 }}>
          <div className="card-h">
            <ShieldCheck size={16} className="ci" />
            <h3>Treaties they are on</h3>
            <span className="sub">as lead reinsurer, or holding a share of a layer</span>
          </div>
          <div className="tbl-wrap">
            <table>
              <thead>
                <tr><th>Treaty</th><th>Type</th><th>Term</th><th>Their lines</th><th>State</th></tr>
              </thead>
              <tbody>
                {(treaties ?? []).map(t => (
                  <tr key={t.contract_id}>
                    <td>
                      <Link className="linkish" to={`/contracts/${t.contract_id}`}>{t.name}</Link>
                      {t.is_lead_reinsurer && (
                        <span className="badge b-ok" style={{ marginLeft: 8 }}>
                          <span className="d" />Lead
                        </span>
                      )}
                      {t.headline && <div className="sub">{t.headline}</div>}
                    </td>
                    <td>{t.type_label ?? "—"}</td>
                    <td className="sub">{fmtDate(t.inception_dt)} → {fmtDate(t.expiry_dt)}</td>
                    <td>
                      {t.lines.length === 0 ? <span className="sub">No share recorded</span>
                        : t.lines.map((ln, i) => (
                          <div key={i} className="sub">
                            {ln.layer}: <b className="mono">{ln.share_pct}%</b>
                            {ln.is_lead && " · leads"}
                          </div>
                        ))}
                    </td>
                    <td>
                      <span className={`badge ${LIFE[t.lifecycle] ?? "b-mut"}`}>
                        <span className="d" />{t.lifecycle.replace(/_/g, " ")}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {treaties?.length === 0 && (
              <div className="empty">Not on any treaty yet.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
