/**
 * Review an endorsement uploaded on a treaty — read for what it changes,
 * checked against the document, then applied.
 *
 *   left   the endorsement itself, page by page
 *   right  its effective date; each term of the treaty it changes — from the
 *          value in force just before its date, to the new one — and every
 *          clause it adds or amends, each with the page it was read from
 *
 * Confirming makes each term a dated version from the effective date (source
 * Endorsement, linked to this document) and puts its clauses among the
 * treaty's. Confirming again replaces what it applied before.
 */
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft, CheckCircle2, ChevronLeft, ChevronRight, FileText, Loader2, Plus, Sparkles, Trash2,
} from "lucide-react";
import { fieldErrors } from "../api/contractRecord";
import {
  confirmEndorsement, documentPageUrl, getEndorsement, readEndorsement,
  type EndorsementChangeRow, type EndorsementClauseRow, type EndorsementRecord,
  type ProfitCommission, type SlidingScale,
} from "../api/treaty";
import {
  DEFAULT_PROFIT, DEFAULT_SCALE, ProfitCommissionEditor, SlidingScaleEditor, profitText, scaleText,
} from "../components/treaty/CommissionEditors";
import { fmtDate, fmtStamp } from "../utils/date";

const key = (t: { term: string; layer: number | null }) => `${t.term}:${t.layer ?? ""}`;

function showValue(kind: string | undefined, v: unknown): string {
  if (v == null || v === "") return "—";
  if (kind === "sliding") return scaleText(v as SlidingScale);
  if (kind === "profit") return profitText(v as ProfitCommission);
  if (kind === "pct") return `${v}%`;
  if (kind === "money") return Number(v).toLocaleString();
  return String(v);
}

export default function TreatyEndorsementReview() {
  const { contractId, docId } = useParams();
  const cid = Number(contractId), did = Number(docId);
  const [rec, setRec] = useState<EndorsementRecord | null>(null);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"" | "read" | "confirm">("");
  const [date, setDate] = useState("");
  const [number, setNumber] = useState("");
  const [changes, setChanges] = useState<EndorsementChangeRow[]>([]);
  const [clauses, setClauses] = useState<EndorsementClauseRow[]>([]);
  const [page, setPage] = useState(1);
  const [img, setImg] = useState<string | null>(null);
  const [quote, setQuote] = useState<{ page: number; text: string | null } | null>(null);

  function seed(r: EndorsementRecord) {
    setRec(r);
    // Unconfirmed reading first; else what was confirmed.
    const fresh = r.reading && (!r.review || r.reading.read_at > r.review.confirmed_at);
    const src = fresh ? r.reading : r.review;
    setDate(src?.effective_date ?? r.document.effective_from ?? "");
    setNumber((fresh ? r.reading?.endorsement_number : r.review?.endorsement_number) ?? "");
    setChanges((src?.changes ?? []).map(c => ({ ...c })));
    setClauses((src?.clauses ?? []).map(c => ({ ...c })));
  }

  async function doRead() {
    setBusy("read"); setErr(""); setNote("");
    try {
      const r = await readEndorsement(cid, did);
      seed(r);
      const rd = r.reading!;
      setNote(`Read — ${rd.changes.length} term change${rd.changes.length === 1 ? "" : "s"} and `
        + `${rd.clauses.length} clause${rd.clauses.length === 1 ? "" : "s"}, each with its page. `
        + "Check them against the document, correct anything, then apply.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  useEffect(() => {
    getEndorsement(cid, did).then(r => {
      seed(r);
      // Read it straight away the first time it is opened.
      if (!r.reading && !r.review && !r.document.drafted_here && r.can_edit
          && r.document.validation.status === "valid") void doRead();
    }).catch(e => setErr(fieldErrors(e).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cid, did]);

  const pages = rec?.document.validation.pages ?? 0;
  useEffect(() => {
    if (!rec || !pages) return;
    let url: string | null = null;
    setImg(null);
    documentPageUrl(cid, did, page).then(u => { url = u; setImg(u); }).catch(() => setImg(null));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [cid, did, page, rec, pages]);

  async function doConfirm() {
    setBusy("confirm"); setErr(""); setNote("");
    try {
      const r = await confirmEndorsement(cid, did, {
        effective_date: date || null, endorsement_number: number || null,
        changes: changes.map(({ term, layer, value, page: p, quote: q }) => ({ term, layer, value, page: p, quote: q })),
        clauses });
      seed(r);
      const n = r.applied.length, k = clauses.length;
      const others = r.reading?.other_changes.length ?? 0;
      const nr = r.rules.length;
      setNote(`Applied from ${date ? fmtDate(date) : "its date"}: `
        + (n ? `${n} calculation term${n === 1 ? "" : "s"} changed on the Calculation basis`
             : "it changes none of the treaty's calculation terms (cession, commission, limits)")
        + `; ${k} clause${k === 1 ? "" : "s"} added to the treaty's clauses`
        + (nr ? `, giving ${nr} rule${nr === 1 ? "" : "s"} checked on the reinsurance bordereau from that date.`
              : ".")
        + (others ? ` It also changes ${others} other term${others === 1 ? "" : "s"} — see `
                    + "“Its other changes” below." : ""));
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(Object.values(fe.errors).join(" ") || fe.message);
    } finally {
      setBusy("");
    }
  }

  if (!rec) {
    return <div className="proto"><div className="view full">
      {err ? <div className="note warn">{err}</div> : <div className="sub">Loading…</div>}
    </div></div>;
  }
  const termOf = (c: EndorsementChangeRow) => rec.terms.find(t => key(t) === key(c));
  const ro = !rec.can_edit;
  const Src = ({ p, q }: { p?: number | null; q?: string | null }) => p ? (
    <button type="button" className="badge b-info" style={{ border: 0, cursor: "pointer" }}
            title={q ? `“${q}”` : `Page ${p}`}
            onClick={() => { setPage(p); setQuote({ page: p, text: q ?? null }); }}>
      <span className="d" />p. {p}
    </button>) : <span className="sub">—</span>;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Review endorsement</h2>
            <p>{rec.treaty} · {rec.document.filename}</p>
          </div>
          <div className="actions">
            <Link className="btn" to={`/contracts/${cid}`}><ArrowLeft size={14} /> Back to treaty</Link>
            {!ro && rec.document.validation.status === "valid" && (
              <button className="btn" type="button" disabled={!!busy} onClick={doRead}>
                {busy === "read" ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
                {" "}{rec.reading ? "Read it again" : "Read the endorsement"}
              </button>
            )}
            {!ro && (
              <button className="btn pri" type="button" disabled={!!busy} onClick={doConfirm}>
                {busy === "confirm" ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
                {" "}{rec.review ? "Apply again" : "Apply to the treaty"}
              </button>
            )}
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
        {note && <div className="note ok" style={{ marginBottom: 14 }}>{note}</div>}
        {busy === "read" && (
          <div className="note" style={{ marginBottom: 14 }}>
            <Loader2 size={13} className="animate-spin" /> Reading the endorsement for the terms it
            changes and its clauses…
          </div>
        )}
        {rec.document.drafted_here && (
          <div className="note" style={{ marginBottom: 14 }}>
            Drafted in Kavachio — its changes were applied to the treaty when it was made.
          </div>
        )}
        {rec.review && (
          <div className="note ok" style={{ marginBottom: 14 }}>
            <b>Applied</b> by {rec.review.confirmed_by ?? "—"} on {fmtStamp(rec.review.confirmed_at)}
            {rec.review.effective_date && <>, from {fmtDate(rec.review.effective_date)}</>}:{" "}
            {rec.applied.length
              ? `${rec.applied.length} calculation term${rec.applied.length === 1 ? "" : "s"} on the Calculation basis`
              : "no calculation term changed"},{" "}
            {rec.review.clauses.length} clause{rec.review.clauses.length === 1 ? "" : "s"} added.
          </div>
        )}
        {rec.document.validation.status !== "valid" && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            {rec.document.validation.label}. Enter its changes and clauses by hand below.
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 18 }}
             className="wording-review">
          {/* ── the endorsement ── */}
          <div className="card" style={{ alignSelf: "start", position: "sticky", top: 12 }}>
            <div className="card-h">
              <FileText size={16} className="ci" />
              <h3 style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{rec.document.filename}</h3>
              {rec.document.version && <span className="right tag-pill">{rec.document.version}</span>}
            </div>
            {quote && quote.page === page && quote.text && (
              <div className="note" style={{ margin: "10px 20px" }}>
                <b>Page {quote.page}</b> — &ldquo;{quote.text}&rdquo;
              </div>
            )}
            <div style={{ padding: "0 20px", background: "var(--p-surface-2)", minHeight: 420,
                          display: "grid", placeItems: "center" }}>
              {img ? <img src={img} alt={`Page ${page}`} style={{ width: "100%", display: "block",
                       border: "1px solid var(--p-border)", margin: "12px 0" }} />
                   : <span className="sub">{pages ? "Loading page…" : "No pages to show."}</span>}
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 20px" }}>
              <button className="btn sm" type="button" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>
                <ChevronLeft size={13} /> Previous</button>
              <span className="sub" style={{ flex: 1, textAlign: "center" }}>Page {page} of {pages || "—"}</span>
              <button className="btn sm" type="button" disabled={page >= pages} onClick={() => setPage(p => p + 1)}>
                Next <ChevronRight size={13} /></button>
            </div>
          </div>

          <div style={{ display: "grid", gap: 14, alignSelf: "start" }}>
            {/* ── when ── */}
            <div className="card pad">
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                <div className="field" style={{ marginBottom: 0, width: 190 }}>
                  <label>Takes effect from</label>
                  <input type="date" value={date} disabled={ro} onChange={e => setDate(e.target.value)} />
                </div>
                <div className="field" style={{ marginBottom: 0, width: 160 }}>
                  <label>Endorsement number</label>
                  <input value={number} disabled={ro} onChange={e => setNumber(e.target.value)} placeholder="e.g. 2" />
                </div>
              </div>
            </div>

            {/* ── the terms it changes ── */}
            <div className="card">
              <div className="card-h"><h3>Terms it changes</h3>
                <span className="sub">each applies from {date ? fmtDate(date) : "its date"}</span></div>
              <div className="tbl-wrap">
                <table className="panel-tbl" style={{ textAlign: "left" }}>
                  <thead><tr><th style={{ width: "30%" }}>Term</th><th>Before</th><th>From this endorsement</th>
                    <th style={{ width: 60 }}>Source</th>{!ro && <th style={{ width: 40 }} />}</tr></thead>
                  <tbody>
                    {changes.map((c, i) => {
                      const t = termOf(c);
                      const kind = t?.kind;
                      const set = (patch: Partial<EndorsementChangeRow>) =>
                        setChanges(cs => cs.map((x, j) => j === i ? { ...x, ...patch } : x));
                      return (
                        <tr key={i} style={{ verticalAlign: "top" }}>
                          <td>
                            <select value={key(c)} disabled={ro} onChange={e => {
                              const nt = rec.terms.find(x => key(x) === e.target.value)!;
                              set({ term: nt.term, layer: nt.layer,
                                    value: nt.kind === "sliding" ? DEFAULT_SCALE
                                      : nt.kind === "profit" ? DEFAULT_PROFIT : "" });
                            }}>
                              {rec.terms.map(x => <option key={key(x)} value={key(x)}>
                                {x.label}{x.layer_name ? ` — ${x.layer_name}` : ""}</option>)}
                            </select>
                          </td>
                          <td className="sub">{showValue(kind, t?.value_before)}</td>
                          <td>
                            {kind === "sliding"
                              ? <SlidingScaleEditor value={(c.value as SlidingScale) ?? DEFAULT_SCALE}
                                  disabled={ro} onChange={v => set({ value: v })} />
                              : kind === "profit"
                                ? <ProfitCommissionEditor value={(c.value as ProfitCommission) ?? DEFAULT_PROFIT}
                                    disabled={ro} onChange={v => set({ value: v })} />
                                : <input inputMode="decimal" disabled={ro} value={c.value == null ? "" : String(c.value)}
                                    onChange={e => set({ value: e.target.value })}
                                    placeholder={kind === "pct" ? "new %" : "new value"} />}
                          </td>
                          <td><Src p={c.page} q={c.quote} /></td>
                          {!ro && <td><button className="btn sm ghost" type="button" aria-label="Remove"
                                    onClick={() => setChanges(cs => cs.filter((_, j) => j !== i))}>
                                    <Trash2 size={12} /></button></td>}
                        </tr>
                      );
                    })}
                    {changes.length === 0 && (
                      <tr><td colSpan={5} className="sub">
                        It changes none of the treaty&rsquo;s calculation terms — cession, commission,
                        lines, limits. {rec.reading?.other_changes.length
                          ? "Its other changes are listed below." : ""}</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              {!ro && rec.terms.length > 0 && (
                <div style={{ padding: "10px 20px" }}>
                  <button className="btn sm" type="button" onClick={() => setChanges(cs => [...cs, {
                    term: rec.terms[0].term, layer: rec.terms[0].layer, value: "" }])}>
                    <Plus size={12} /> Add a term it changes</button>
                </div>
              )}
              {!!rec.reading?.other_changes.length && (
                <div className="note" style={{ margin: "0 20px 14px" }}>
                  <b>Its other changes</b> — read from it, not applied by this page:
                  <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                    {rec.reading.other_changes.map((o, i) => (
                      <li key={i}>{o.text}{o.page ? ` (page ${o.page})` : ""}</li>))}
                  </ul>
                  <div className="sub" style={{ marginTop: 4 }}>
                    They are not calculation terms (or the value could not be read soundly). Their
                    wording is among the clauses above; a change to the treaty&rsquo;s period is made
                    with <b>Change period</b> on the treaty, and to how often accounts are due on its
                    reinsurance bordereau setup.
                  </div>
                </div>
              )}
            </div>

            {/* ── the rules its clauses give ── */}
            {(rec.review || rec.rules.length > 0) && (
              <div className="card">
                <div className="card-h"><h3>Rules read from its clauses</h3>
                  <span className="sub">checked on the reinsurance bordereau from {
                    rec.review?.effective_date ? fmtDate(rec.review.effective_date) : "its date"}</span></div>
                <div style={{ padding: "10px 20px" }}>
                  {rec.rules_error && <div className="note warn" style={{ marginBottom: 8 }}>{rec.rules_error}</div>}
                  {rec.rules.length === 0 ? (
                    <div className="sub">
                      {rec.rules_error ? "" : "None — its clauses set no condition the ceded business must meet."}
                    </div>
                  ) : rec.rules.map(r => (
                    <div key={r.id} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "4px 0" }}>
                      <span className={`badge ${r.severity === "critical" ? "b-crit" : "b-warn"}`}>
                        <span className="d" />{r.severity === "critical" ? "Critical" : "Warning"}</span>
                      <span>
                        <b style={{ fontWeight: 600 }}>{r.label}</b>
                        {r.clause && <div className="sub"><i>&ldquo;{r.clause}&rdquo;</i></div>}
                      </span>
                    </div>
                  ))}
                  {rec.rules.length > 0 && (
                    <div className="sub" style={{ marginTop: 6 }}>
                      They are among the treaty&rsquo;s rules on its reinsurance bordereau setup
                      (Treaty Rules → Endorsements), where each can be set to Critical or Warning,
                      or switched off.
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* ── its clauses ── */}
            <div className="card">
              <div className="card-h"><h3>Clauses it adds or amends</h3>
                <span className="sub">added to the treaty&rsquo;s clauses</span></div>
              <div style={{ padding: "10px 20px", display: "grid", gap: 10 }}>
                {clauses.map((cl, i) => (
                  <div key={i} className="card pad" style={{ boxShadow: "none" }}>
                    <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
                      <input value={cl.title} disabled={ro} style={{ flex: 1 }}
                             onChange={e => setClauses(cs => cs.map((x, j) => j === i ? { ...x, title: e.target.value } : x))} />
                      <Src p={cl.page} />
                      {!ro && <button className="btn sm ghost" type="button" aria-label="Remove clause"
                                onClick={() => setClauses(cs => cs.filter((_, j) => j !== i))}><Trash2 size={12} /></button>}
                    </div>
                    <textarea rows={4} value={cl.text} disabled={ro} style={{ width: "100%" }}
                              onChange={e => setClauses(cs => cs.map((x, j) => j === i ? { ...x, text: e.target.value } : x))} />
                  </div>
                ))}
                {clauses.length === 0 && <div className="sub">No clause read from it.</div>}
                {!ro && (
                  <button className="btn sm" type="button" style={{ width: "fit-content" }}
                          onClick={() => setClauses(cs => [...cs, { title: "", text: "" }])}>
                    <Plus size={12} /> Add a clause</button>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
