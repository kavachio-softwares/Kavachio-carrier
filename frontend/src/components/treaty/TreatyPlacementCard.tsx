/**
 * A MASTER treaty's placement and signatures — one treaty, one shared wording,
 * several reinsurers each writing its own participation share of each layer,
 * and each SIGNING for itself.
 *
 *   Layers       written (unplaced · partially · fully · over-placed), signed
 *                (unsigned · partially · fully signed) and what is retained.
 *   Reinsurers   its lines, its signature — on its private link, in the
 *                signing round (the lead), or on paper recorded against
 *                document evidence — and what to do next: ask it to sign,
 *                record a paper signature with its signed line slip, withdraw.
 */
import { useEffect, useState } from "react";
import { FileText, Loader2, PenLine, Send, Undo2, Upload, Users } from "lucide-react";
import { InfoTip } from "../InfoTip";
import { showDone, showError } from "../ResultPopup";
import { downloadDocument, fieldErrors, uploadDocument } from "../../api/contractRecord";
import {
  getPlacement, getReinsurerAccess, recordPartySignature, shareWithReinsurers,
  withdrawPartySignature, type Placement, type PlacementLine, type ShareableParty,
} from "../../api/treaty";
import { fmtDate } from "../../utils/date";

const WRITTEN_TONE: Record<string, string> = {
  unplaced: "b-mut", partially_placed: "b-warn", fully_placed: "b-ok", over_placed: "b-crit" };
const SIGNED_TONE: Record<string, string> = {
  unsigned: "b-mut", partially_signed: "b-warn", fully_signed: "b-ok" };

type Party = { party_id: number; name: string | null; is_lead: boolean;
               lines: { layer: string; share_pct: number }[]; line: PlacementLine };

export default function TreatyPlacementCard({ contractId, canAct, onChanged }: {
  contractId: number; canAct: boolean; onChanged?: () => void;
}) {
  const [pl, setPl] = useState<Placement | null>(null);
  const [contacts, setContacts] = useState<ShareableParty[]>([]);
  // Only a failed load: an action's result is a popup (showDone / showError).
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState("");
  const [open, setOpen] = useState<{ pid: number; what: "ask" | "record" } | null>(null);
  const [form, setForm] = useState({ contact: "", name: "", title: "", email: "", doc: "" });

  const load = () => getPlacement(contractId).then(setPl).catch(e => setErr(fieldErrors(e).message));
  useEffect(() => {
    load();
    if (canAct) getReinsurerAccess(contractId).then(a => setContacts(a.contacts)).catch(() => setContacts([]));
  }, [contractId, canAct]);

  async function act(label: string, fn: () => Promise<unknown>, done: string) {
    setBusy(label);
    try {
      await fn();
      await load();
      setOpen(null);
      showDone(done);
      onChanged?.();
    } catch (e) {
      showError(e);
    } finally {
      setBusy("");
    }
  }

  if (!pl) return err ? <div className="note warn" style={{ marginBottom: 14 }}>{err}</div> : null;
  if (!pl.has_structure) return null;

  // Each reinsurer once, with its lines across the layers.
  const parties: Party[] = [];
  for (const lay of pl.layers) {
    for (const ln of lay.lines) {
      if (ln.party_id == null) continue;
      let p = parties.find(x => x.party_id === ln.party_id);
      if (!p) { p = { party_id: ln.party_id, name: ln.name, is_lead: ln.is_lead, lines: [], line: ln }; parties.push(p); }
      p.lines.push({ layer: lay.name, share_pct: ln.share_pct });
      p.is_lead = p.is_lead || ln.is_lead;
    }
  }
  const evidenceFor = (pid: number) => [
    ...(pl.evidence[String(pid)] ?? []).map(d => ({ ...d, label: `${d.filename} — its signed line slip` })),
    ...(pl.wording?.document_id ? [{ document_id: pl.wording.document_id, filename: pl.wording.filename,
                                     kind: "contract", label: `${pl.wording.filename} — the signed treaty wording` }] : []),
  ];

  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-h" style={{ flexWrap: "wrap", gap: 8 }}>
        <Users size={16} className="ci" />
        <h3>Placement &amp; signatures</h3>
        <InfoTip text="A master treaty: one treaty and one shared wording, placed with several reinsurers. Each writes its own share of each layer and signs for itself — on its private link, or on paper and recorded here against its signed line slip. The lead reinsurer's signature, with yours, puts the treaty in force; the others bind their own lines." />
        <span className="right" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <span className={`badge ${WRITTEN_TONE[pl.written_status]}`}><span className="d" />{pl.written_label}</span>
          <span className={`badge ${SIGNED_TONE[pl.signed_status]}`}><span className="d" />{pl.signed_label}</span>
          <span className="badge b-mut">{pl.parties_signed} of {pl.parties} reinsurers signed</span>
        </span>
      </div>
      <div style={{ padding: "12px 20px" }}>
        <div className="sub" style={{ marginBottom: 10 }}>
          <FileText size={12} style={{ verticalAlign: "-2px" }} />{" "}
          {pl.wording
            ? <>One shared wording for every reinsurer: <b>{pl.wording.filename}</b>
                {pl.wording.version ? ` (${pl.wording.version})` : ""}.</>
            : <>No signed treaty wording yet — every reinsurer signs the same wording, so attach it first.</>}
        </div>
        {/* ── each layer: written, signed, retained ── */}
        <div className="tbl-wrap" style={{ marginBottom: 14 }}>
          <table className="panel-tbl">
            <thead><tr><th>Layer</th><th>Written</th><th>Signed</th><th>Retained by you</th></tr></thead>
            <tbody>
              {pl.layers.map(l => (
                <tr key={l.index}>
                  <td><b style={{ fontWeight: 600 }}>{l.name}</b></td>
                  <td>
                    <span className={`badge ${WRITTEN_TONE[l.written_status]}`}><span className="d" />{l.written_label}</span>
                    <div className="sub">{l.written_pct}% of the layer</div>
                  </td>
                  <td>
                    <span className={`badge ${SIGNED_TONE[l.signed_status]}`}><span className="d" />{l.signed_label}</span>
                    <div className="sub">{l.signed_pct}% signed</div>
                  </td>
                  <td className="mono">{l.retained_pct}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {/* ── each reinsurer: its lines, signature, evidence ── */}
        <div className="tbl-wrap">
          <table className="panel-tbl">
            <thead><tr><th>Reinsurer</th><th>Participation</th><th>Signature</th><th>Evidence</th>
              {canAct && <th />}</tr></thead>
            <tbody>
              {parties.map(p => {
                const sig = p.line.signature;
                const why = pl.can_record[String(p.party_id)];
                const opened = open?.pid === p.party_id ? open.what : null;
                const people = contacts.find(c => c.party_id === p.party_id)?.contacts.filter(c => c.email) ?? [];
                return (
                  <tr key={p.party_id}>
                    <td>
                      <b style={{ fontWeight: 600 }}>{p.name}</b>
                      {p.is_lead && <span className="badge b-info" style={{ marginLeft: 6 }}>Lead</span>}
                    </td>
                    <td className="mono" style={{ fontSize: 12 }}>
                      {p.lines.map(x => <div key={x.layer}>{x.layer}: {x.share_pct}%</div>)}
                    </td>
                    <td>
                      {sig ? (
                        <>
                          <span className={`badge ${sig.stale ? "b-warn" : "b-ok"}`}><span className="d" />
                            {sig.stale ? "Line changed since signed" : "Signed"}</span>
                          <div className="sub">{sig.signer_name}{sig.signer_title ? `, ${sig.signer_title}` : ""}
                            {sig.signed_at ? ` · ${fmtDate(sig.signed_at)}` : ""}</div>
                          <div className="sub">{sig.method_label}</div>
                        </>
                      ) : (
                        <>
                          <span className="badge b-mut"><span className="d" />Awaiting signature</span>
                          {why && <div className="sub">{why.charAt(0).toUpperCase() + why.slice(1)}</div>}
                        </>
                      )}
                    </td>
                    <td>
                      {sig?.evidence ? (
                        <button type="button" className="linkish" disabled={!sig.evidence.has_file}
                                style={{ background: "none", border: 0, padding: 0, cursor: "pointer", textAlign: "left" }}
                                onClick={() => downloadDocument(contractId, sig.evidence!.document_id, sig.evidence!.filename)}>
                          <FileText size={12} style={{ verticalAlign: "-2px" }} /> {sig.evidence.filename}
                        </button>
                      ) : sig?.method === "link" ? <span className="sub">Its code-verified signature on its link</span>
                        : sig ? <span className="sub">The signed treaty</span>
                        : (pl.evidence[String(p.party_id)] ?? []).length
                          ? <span className="sub">{pl.evidence[String(p.party_id)].length} slip(s) on file</span>
                          : <span className="sub">—</span>}
                    </td>
                    {canAct && (
                      <td style={{ minWidth: 260 }}>
                        {!sig && !why && !opened && (
                          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                            <button className="btn sm" type="button" onClick={() => {
                              setOpen({ pid: p.party_id, what: "ask" });
                              setForm({ ...form, contact: String(people[0]?.id ?? "") });
                            }}><Send size={12} /> Ask to sign</button>
                            <button className="btn sm" type="button" onClick={() => {
                              setOpen({ pid: p.party_id, what: "record" });
                              setForm({ contact: "", name: "", title: "", email: "",
                                        doc: String(evidenceFor(p.party_id)[0]?.document_id ?? "") });
                            }}><PenLine size={12} /> Record paper signature</button>
                          </div>
                        )}
                        {sig && !p.is_lead && (
                          <button className="btn sm" type="button" disabled={!!busy}
                                  onClick={() => act("withdraw", () => withdrawPartySignature(contractId, p.party_id),
                                    `${p.name}'s signature withdrawn.`)}>
                            <Undo2 size={12} /> Withdraw
                          </button>
                        )}
                        {opened === "ask" && (
                          <div className="field" style={{ margin: 0 }}>
                            {people.length ? (
                              <>
                                <select value={form.contact} onChange={e => setForm({ ...form, contact: e.target.value })}>
                                  {people.map(c => <option key={c.id} value={c.id}>{c.name} — {c.email}</option>)}
                                </select>
                                <div className="rowacts" style={{ marginTop: 6 }}>
                                  <button className="btn sm pri" type="button" disabled={!form.contact || !!busy}
                                          onClick={() => act("ask", () => shareWithReinsurers(contractId, [Number(form.contact)], true),
                                            `${p.name} has been emailed a private link to sign its lines.`)}>
                                    {busy === "ask" ? <Loader2 size={12} className="animate-spin" /> : <Send size={12} />} Send
                                  </button>
                                  <button className="btn sm" type="button" onClick={() => setOpen(null)}>Cancel</button>
                                </div>
                              </>
                            ) : <span className="sub">No contact with an email — add one on {p.name}&rsquo;s page.</span>}
                          </div>
                        )}
                        {opened === "record" && (
                          <div style={{ display: "grid", gap: 6 }}>
                            <input placeholder="Who signed for them" value={form.name}
                                   onChange={e => setForm({ ...form, name: e.target.value })} />
                            <input placeholder="Their title (optional)" value={form.title}
                                   onChange={e => setForm({ ...form, title: e.target.value })} />
                            <select value={form.doc} onChange={e => setForm({ ...form, doc: e.target.value })}>
                              <option value="">Evidence — choose the signed document…</option>
                              {evidenceFor(p.party_id).map(d => (
                                <option key={d.document_id} value={d.document_id}>{d.label}</option>))}
                            </select>
                            <label className="btn sm" style={{ width: "fit-content", cursor: "pointer" }}>
                              <Upload size={12} /> Upload its signed line slip
                              <input type="file" hidden onChange={async e => {
                                const f = e.target.files?.[0]; e.target.value = "";
                                if (!f) return;
                                setBusy("upload");
                                try {
                                  const r = await uploadDocument(contractId, f, { kind: "signed_line", partyId: p.party_id });
                                  await load();
                                  setForm(x => ({ ...x, doc: String(r.document.id) }));
                                } catch (er) {
                                  showError(er);
                                } finally { setBusy(""); }
                              }} />
                            </label>
                            <div className="rowacts">
                              <button className="btn sm pri" type="button"
                                      disabled={!form.name.trim() || !form.doc || !!busy}
                                      onClick={() => act("record", () => recordPartySignature(contractId, p.party_id, {
                                        signer_name: form.name.trim(), signer_title: form.title.trim() || null,
                                        document_id: Number(form.doc) }),
                                        `${p.name}'s signature recorded against its evidence.`)}>
                                {busy === "record" ? <Loader2 size={12} className="animate-spin" /> : <PenLine size={12} />} Record
                              </button>
                              <button className="btn sm" type="button" onClick={() => setOpen(null)}>Cancel</button>
                            </div>
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                );
              })}
              {parties.length === 0 && (
                <tr><td colSpan={canAct ? 5 : 4}><div className="empty">No reinsurer has a line yet.</div></td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
