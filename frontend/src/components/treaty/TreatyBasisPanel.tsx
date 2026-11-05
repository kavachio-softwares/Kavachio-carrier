/**
 * A treaty's CALCULATION BASIS, with the dates each term applies from
 * (27.1, 27.2).
 *
 * Every term its figures are worked out from — cession, a surplus's lines,
 * each layer's attachment and limit, and the ceding, sliding-scale and profit
 * commission — as a timeline: the value agreed on the structure from
 * inception, then each change from its own date. A statement is worked out
 * with the terms in force on its period's first day.
 *
 * The sliding scale and the profit commission are the treaty's own formulas
 * (EXT): entered here as their figures, and shown working on sample figures
 * before they are saved.
 */
import { useEffect, useState } from "react";
import { CalendarClock, Calculator, CheckCircle2, Loader2, Plus, Send, Trash2 } from "lucide-react";
import { InfoTip } from "../InfoTip";
import {
  DEFAULT_PROFIT, DEFAULT_SCALE, ProfitCommissionEditor, SlidingScaleEditor, profitText, scaleText,
} from "./CommissionEditors";
import { fieldErrors } from "../../api/contractRecord";
import {
  addBasis, evaluateBasis, getBasis, removeBasis, sendBasisForApproval, type Basis,
  type BasisChangeStatus, type BasisTerm, type BasisVersion, type ProfitCommission,
  type SlidingScale,
} from "../../api/treaty";
import { fmtDate, fmtStamp } from "../../utils/date";

/** A change entered here, with the reinsurers. */
const CHANGE: Record<BasisChangeStatus, [string, string]> = {
  draft: ["b-mut", "Draft — not sent"],
  pending: ["b-warn", "Waiting for approval"],
  changes_requested: ["b-crit", "Changes requested"],
  approved: ["b-ok", "Approved"],
};
const ANSWER: Record<string, [string, string]> = {
  pending: ["b-mut", "Not answered"],
  approved: ["b-ok", "Approved"],
  changes_requested: ["b-crit", "Asked for changes"],
};

const SOURCE: Record<string, string> = {
  structure: "As agreed", manual: "Changed", wording: "From the wording", endorsement: "Endorsement",
};
const BASIS_TERMS = new Set(["cession_pct", "retention_line", "lines", "retention", "limit",
  "attachment_pct", "limit_pct", "attachment_amount", "limit_amount", "share_ceded_pct"]);

const money = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 2 });

function show(t: BasisTerm, v: BasisVersion["value"]): string {
  if (t.kind === "pct") return `${v}%`;
  if (t.kind === "money") return money(Number(v));
  if (t.kind === "int") return String(v);
  if (t.kind === "sliding") return scaleText(v as SlidingScale);
  return profitText(v as ProfitCommission);
}

/** The version in force today. */
function current(t: BasisTerm): BasisVersion | undefined {
  const today = new Date().toISOString().slice(0, 10);
  return [...t.versions].filter(v => !v.pending && (!v.effective_from || v.effective_from <= today)).pop()
    ?? t.versions.find(v => !v.pending);
}

export default function TreatyBasisPanel({ contractId }: { contractId: number }) {
  const [b, setB] = useState<Basis | null>(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  useEffect(() => { getBasis(contractId).then(setB).catch(e => setErr(fieldErrors(e).message)); },
            [contractId]);

  if (!b) return err ? <div className="note warn">{err}</div> : <div className="sub">Loading…</div>;
  if (!b.treaty_type) {
    return <div className="note">Set out the treaty&rsquo;s structure first — its terms are the basis.</div>;
  }
  const key = (t: BasisTerm) => `${t.term}:${t.layer ?? ""}`;
  const basis = b.terms.filter(t => BASIS_TERMS.has(t.term));
  const commission = b.terms.filter(t => !BASIS_TERMS.has(t.term));

  async function send() {
    setErr(""); setNote(""); setSending(true);
    try {
      const r = await sendBasisForApproval(contractId);
      setB(r);
      const failed = r.sent.filter(x => !x.emailed).length;
      setNote(`Sent for approval to ${r.sent.length} ${r.sent.length === 1 ? "person" : "people"} `
        + `at the reinsurers.`
        + (failed ? ` ${failed} email${failed === 1 ? "" : "s"} could not be sent — send again.` : "")
        + (r.missing.length ? ` No contact with an email at ${r.missing.join(", ")}.` : ""));
    } catch (e) { setErr(fieldErrors(e).message); }
    finally { setSending(false); }
  }

  async function remove(id: number) {
    setErr(""); setNote("");
    try { setB(await removeBasis(contractId, id)); setNote("Change removed."); }
    catch (e) { setErr(fieldErrors(e).message); }
  }

  const section = (title: string, tip: string, terms: BasisTerm[]) => (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-h">
        <CalendarClock size={16} className="ci" />
        <h3>{title}</h3>
        <InfoTip text={tip} />
      </div>
      <div className="tbl-wrap">
        <table className="panel-tbl" style={{ textAlign: "left" }}>
          <thead><tr><th style={{ width: "22%" }}>Term</th><th style={{ width: "22%" }}>In force today</th>
            <th>Applies from</th>{b.can_edit && <th style={{ width: 150 }} />}</tr></thead>
          <tbody>
            {terms.map(t => {
              const cur = current(t);
              return (
                <tr key={key(t)} style={{ verticalAlign: "top" }}>
                  <td>
                    <b style={{ fontWeight: 600 }}>{t.label}</b>
                    {t.layer_name && <div className="sub">{t.layer_name}</div>}
                  </td>
                  <td>{cur ? <b className="mono" style={{ fontWeight: 600 }}>{show(t, cur.value)}</b>
                       : <span className="sub">Not set</span>}</td>
                  <td>
                    {t.versions.length === 0 && <span className="sub">Not set on the treaty</span>}
                    {t.versions.map((v, i) => (
                      <div key={v.id ?? `s${i}`} style={{ display: "flex", gap: 8, alignItems: "baseline",
                                                          flexWrap: "wrap", padding: "3px 0",
                                                          opacity: v === cur ? 1 : .75 }}>
                        <span className="mono" style={{ minWidth: 92 }}>
                          {v.effective_from ? fmtDate(v.effective_from) : "Inception"}</span>
                        <span>{show(t, v.value)}</span>
                        <span className="badge b-mut" style={{ fontSize: 10.5 }}>{SOURCE[v.source] ?? v.source}</span>
                        {v === cur && <span className="badge b-ok" style={{ fontSize: 10.5 }}>In force</span>}
                        {v.approval ? (
                          <span className={`badge ${CHANGE[v.approval][0]}`} style={{ fontSize: 10.5 }}
                                title={v.approval === "approved" ? undefined
                                  : "Not used until every reinsurer on the treaty has approved it"}>
                            {CHANGE[v.approval][1]}</span>
                        ) : v.pending && (
                          <span className="badge b-warn" style={{ fontSize: 10.5 }}
                                title="Not used until every reinsurer on the treaty has approved and signed the endorsement">
                            Waiting for approval</span>)}
                        {(v.note || v.entered_by) && (
                          <span className="sub">{[v.note, v.entered_by && `by ${v.entered_by}`].filter(Boolean).join(" · ")}</span>)}
                        {b.can_edit && v.id != null && v.approval !== "approved" && (
                          <button className="btn sm ghost" type="button" title="Remove this change"
                                  onClick={() => remove(v.id!)}><Trash2 size={12} /></button>
                        )}
                      </div>
                    ))}
                    {open === key(t) && (
                      <ChangeForm contractId={contractId} t={t} base={cur?.value}
                        b={b} onDone={(nb, msg) => { setB(nb); setOpen(null); setNote(msg); setErr(""); }}
                        onCancel={() => setOpen(null)} />
                    )}
                  </td>
                  {b.can_edit && (
                    <td>
                      {open !== key(t) && (
                        <button className="btn sm" type="button" onClick={() => setOpen(key(t))}>
                          <Plus size={12} /> {t.versions.length ? "Change from a date" : "Set from a date"}
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );

  return (
    <div>
      <div className="note" style={{ marginBottom: 14 }}>
        Every figure on this treaty&rsquo;s statements is worked out from these terms. Each holds its
        value from inception and every change from its own date — a statement uses the terms in force
        on its period&rsquo;s first day, so earlier periods keep the terms they were worked out with.
      </div>
      {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
      {note && <div className="note ok" style={{ marginBottom: 14 }}>{note}</div>}
      <ApprovalCard b={b} sending={sending} onSend={send} />
      {basis.length > 0 && section("Calculation basis",
        "The cession, the lines and each layer's attachment and limit — what every ceded figure is worked from.",
        basis)}
      {commission.length > 0 && section("Commission terms",
        "Ceding commission, and the sliding-scale and profit commission the treaty agrees — each with the date it applies from.",
        commission)}
    </div>
  );
}

/** Changes entered here go to every reinsurer on the treaty to approve;
 *  only approved ones are used. Draft → Waiting for approval → Approved. */
function ApprovalCard({ b, sending, onSend }: {
  b: Basis; sending: boolean; onSend: () => void;
}) {
  const a = b.approval;
  if (!a || (a.unapproved === 0 && !a.round)) return null;
  const rd = a.round;
  const stage = rd ? (rd.status === "approved" ? 2 : 1) : 0;
  const canSend = b.can_edit && a.unapproved > 0;
  return (
    <div className="card" style={{ marginBottom: 14 }}>
      <div className="card-h">
        <CheckCircle2 size={16} className="ci" />
        <h3>
          Reinsurers&rsquo; approval of changes
          <InfoTip text={"A change you enter here is a draft and is not used. Send it to the "
            + "reinsurers — once every reinsurer on the treaty has approved it, it is used "
            + "from its date."} />
        </h3>
        {canSend && (
          <div className="right">
            <button className="btn pri sm" type="button" disabled={sending} onClick={onSend}>
              {sending ? <Loader2 size={13} className="animate-spin" /> : <Send size={13} />}
              {" "}{rd ? "Send again" : "Send for approval"}
            </button>
          </div>
        )}
      </div>
      <div style={{ padding: "12px 20px", display: "grid", gap: 10 }}>
        <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
          {["Draft", "Waiting for approval", "In force"].map((label, i) => (
            <span key={label} style={{ display: "contents" }}>
              {i > 0 && <span className="sub">→</span>}
              <span className={`badge ${i < stage ? "b-ok"
                : i === stage ? (rd?.status === "changes_requested" ? "b-crit" : "b-warn") : "b-mut"}`}>
                <span className="d" />
                {i === 1 && rd?.status === "changes_requested" ? "Changes requested" : label}
              </span>
            </span>
          ))}
        </div>
        {a.draft > 0 && (
          <div className="sub">
            {a.draft} change{a.draft === 1 ? " is" : "s are"} not sent yet and not used.
            {rd ? " Send again to add them to the request." : " Send them to the reinsurers to approve."}
          </div>
        )}
        {rd?.status === "changes_requested" && (
          <div className="sub">A reinsurer asked for changes. Remove or correct the change, then send again.</div>
        )}
        {rd && (
          <table className="panel-tbl" style={{ textAlign: "left" }}>
            <thead><tr><th>Reinsurer</th><th>Answer</th></tr></thead>
            <tbody>
              {rd.parties.map(p => {
                const [cls, lbl] = ANSWER[p.status] ?? ANSWER.pending;
                return (
                  <tr key={p.party_id} style={{ verticalAlign: "top" }}>
                    <td>{p.party_name ?? "—"}{p.is_lead && <span className="sub"> · lead</span>}</td>
                    <td>
                      <span className={`badge ${cls}`}><span className="d" />{lbl}</span>
                      {p.signer_name && <div className="sub">{p.signer_name}{p.at ? ` · ${fmtStamp(p.at)}` : ""}</div>}
                      {p.note && <div className="sub" style={{ marginTop: 4 }}>&ldquo;{p.note}&rdquo;</div>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {rd?.sent_at && (
          <div className="sub">
            Sent {fmtStamp(rd.sent_at)}{rd.sent_by ? ` by ${rd.sent_by}` : ""} · {rd.approved} of {rd.total} approved
          </div>
        )}
      </div>
    </div>
  );
}

/** A dated change to one term — a number, or a sliding scale / profit
 *  commission with its figures and a worked example. */
function ChangeForm({ contractId, t, base, b, onDone, onCancel }: {
  contractId: number; t: BasisTerm; base?: BasisVersion["value"]; b: Basis;
  onDone: (b: Basis, msg: string) => void; onCancel: () => void;
}) {
  const [from, setFrom] = useState("");
  const [num, setNum] = useState(typeof base === "number" ? String(base) : "");
  const [why, setWhy] = useState("");
  const [source, setSource] = useState("manual");
  const [scale, setScale] = useState<SlidingScale>(
    t.kind === "sliding" && base ? base as SlidingScale : DEFAULT_SCALE);
  const [pc, setPc] = useState<ProfitCommission>(
    t.kind === "profit" && base ? base as ProfitCommission : DEFAULT_PROFIT);
  const [sample, setSample] = useState({ premium: "100000", losses: "55000", ceding: "30000" });
  const [result, setResult] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const value = t.kind === "sliding" ? scale : t.kind === "profit" ? pc : num;

  async function save() {
    setBusy(true); setErr("");
    try {
      const nb = await addBasis(contractId, { term: t.term, layer: t.layer, value,
        effective_from: from, note: why || null, source });
      onDone(nb, `${t.label}${t.layer_name ? ` (${t.layer_name})` : ""} from ${fmtDate(from)} `
        + `saved as a draft. It is used once every reinsurer approves it — send it for approval.`);
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(Object.values(fe.errors).join(" ") || fe.message);
    } finally { setBusy(false); }
  }

  async function tryIt() {
    setErr("");
    try {
      const r = await evaluateBasis(contractId, {
        term: t.term as "sliding_scale" | "profit_commission", value,
        premium: Number(sample.premium), losses: Number(sample.losses),
        ceding_commission: Number(sample.ceding) });
      setResult(t.kind === "sliding"
        ? `Loss ratio ${r.loss_ratio}% → commission ${r.rate_pct}% = ${money(r.commission)}; `
          + `provisional ${money(r.provisional)} charged, so an adjustment of ${money(r.adjustment)}.`
        : `Profit ${money(r.profit)} (premium − commission − losses − ${money(r.expenses)} expenses`
          + `${r.deficit_brought_forward ? " − deficit b/f" : ""}) → profit commission ${money(r.commission)}`
          + (r.deficit_carried_forward ? `; deficit ${money(r.deficit_carried_forward)} carried forward.` : "."));
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(Object.values(fe.errors).join(" ") || fe.message);
    }
  }

  const n = (v: string) => (v === "" ? 0 : Number(v));
  return (
    <div className="card pad" style={{ marginTop: 8, boxShadow: "none", background: "var(--p-surface-2)" }}>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
        <div className="field" style={{ marginBottom: 0, width: 160 }}>
          <label>Applies from</label>
          <input type="date" value={from} min={b.inception ?? undefined} max={b.expiry ?? undefined}
                 onChange={e => setFrom(e.target.value)} />
        </div>
        {(t.kind === "pct" || t.kind === "money" || t.kind === "int") && (
          <div className="field" style={{ marginBottom: 0, width: 160 }}>
            <label>{t.label}{t.kind === "pct" ? " (%)" : ""}</label>
            <input inputMode="decimal" value={num} onChange={e => setNum(e.target.value)} />
          </div>
        )}
        <div className="field" style={{ marginBottom: 0, width: 160 }}>
          <label>Source</label>
          <select value={source} onChange={e => setSource(e.target.value)}>
            <option value="manual">Changed</option>
            <option value="endorsement">Endorsement</option>
            <option value="wording">From the wording</option>
          </select>
        </div>
        <div className="field" style={{ marginBottom: 0, flex: "1 1 200px" }}>
          <label>Note</label>
          <input value={why} onChange={e => setWhy(e.target.value)} placeholder="e.g. Endorsement 2, clause 4" />
        </div>
      </div>

      {t.kind === "sliding" && (
        <div style={{ marginTop: 12 }}><SlidingScaleEditor value={scale} onChange={setScale} /></div>
      )}
      {t.kind === "profit" && (
        <div style={{ marginTop: 12 }}><ProfitCommissionEditor value={pc} onChange={setPc} /></div>
      )}

      {(t.kind === "sliding" || t.kind === "profit") && (
        <div style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
          <span className="sub" style={{ width: "100%" }}><Calculator size={12} style={{ verticalAlign: "-2px" }} /> Try it on figures for a year:</span>
          <div className="field" style={{ marginBottom: 0, width: 150 }}><label>Premium ceded</label>
            <input inputMode="decimal" value={sample.premium} onChange={e => setSample({ ...sample, premium: e.target.value })} /></div>
          <div className="field" style={{ marginBottom: 0, width: 150 }}><label>Losses incurred</label>
            <input inputMode="decimal" value={sample.losses} onChange={e => setSample({ ...sample, losses: e.target.value })} /></div>
          {t.kind === "profit" && (
            <div className="field" style={{ marginBottom: 0, width: 150 }}><label>Ceding commission</label>
              <input inputMode="decimal" value={sample.ceding} onChange={e => setSample({ ...sample, ceding: e.target.value })} /></div>)}
          <button className="btn sm" type="button" onClick={tryIt}>Work it out</button>
          {result && <div className="note" style={{ width: "100%", margin: 0 }}>{result}</div>}
        </div>
      )}

      {err && <div className="note warn" style={{ marginTop: 10 }}>{err}</div>}
      <div className="rowacts" style={{ marginTop: 10 }}>
        <button className="btn pri sm" type="button" disabled={busy || !from} onClick={save}>
          {busy ? <Loader2 size={12} className="animate-spin" /> : <CalendarClock size={12} />} Save from {from ? fmtDate(from) : "…"}
        </button>
        <button className="btn sm" type="button" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
