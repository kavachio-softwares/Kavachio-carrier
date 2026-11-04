/**
 * Review Wording & Terms — 26.3: each treaty is configured from its signed
 * wording.
 *
 *   A  Source document     the signed wording itself, page by page — its name,
 *                          version, signed-copy status and effective date.
 *   B  Extracted terms     what the wording says, read by AI and editable —
 *                          the fields the treaty's TYPE has (a quota share asks
 *                          for its cession and commission, an XOL for each
 *                          layer's attachment and limit), required ones marked.
 *   C  Source traceability every term carries the page it was read from and
 *                          the words it quotes; clicking one opens that page.
 *
 * Confirming makes the reviewed terms the treaty's structure. The treaty's
 * lifecycle is its own and is not touched.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  ArrowLeft, CheckCircle2, ChevronLeft, ChevronRight, FileText, Loader2, Sparkles,
} from "lucide-react";
import { fieldErrors } from "../api/contractRecord";
import {
  confirmWording, getTreatyTypes, getWording, readWording, wordingPageUrl,
  type TermSources, type TreatyField, type TreatyInput, type TreatyTypeSpec,
  type WordingRecord,
} from "../api/treaty";
import { fmtDate, fmtStamp } from "../utils/date";
import { OptionalCommission } from "../components/treaty/CommissionEditors";
import type { ProfitCommission, SlidingScale } from "../api/treaty";

type Dates = { inception_dt: string; expiry_dt: string; year_of_account: string;
               notice_period_days: string; effective_date: string };

export default function TreatyWordingReview() {
  const { contractId } = useParams();
  const id = Number(contractId);
  const [rec, setRec] = useState<WordingRecord | null>(null);
  const [types, setTypes] = useState<TreatyTypeSpec[]>([]);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState<"" | "read" | "confirm">("");
  // What is on screen: the latest reading not yet confirmed, else the treaty
  // as it stands — edited here before it is confirmed.
  const [draft, setDraft] = useState<TreatyInput | null>(null);
  const [dates, setDates] = useState<Dates>({ inception_dt: "", expiry_dt: "",
    year_of_account: "", notice_period_days: "", effective_date: "" });
  const [sources, setSources] = useState<TermSources>({});
  const [page, setPage] = useState(1);
  const [img, setImg] = useState<string | null>(null);
  const [quote, setQuote] = useState<{ path: string; page: number; quote: string | null } | null>(null);

  function seed(r: WordingRecord) {
    setRec(r);
    const unconfirmed = r.reading && (!r.review || r.reading.read_at > r.review.confirmed_at);
    const src = unconfirmed ? r.reading : null;
    const base = src?.treaty ?? r.structure;
    setDraft(base ? { treaty_type: base.treaty_type, terms: { ...base.terms },
                      layers: base.layers.map(l => ({ ...l, terms: { ...l.terms },
                                                      shares: l.shares.map(x => ({ ...x })) })) } : null);
    setDates({
      inception_dt: src?.inception_dt ?? r.record.inception_dt ?? "",
      expiry_dt: src?.expiry_dt ?? r.record.expiry_dt ?? "",
      year_of_account: src?.year_of_account ?? r.record.year_of_account ?? "",
      notice_period_days: String(src?.notice_period_days ?? r.record.notice_period_days ?? ""),
      effective_date: src?.effective_date ?? r.review?.effective_date ?? "",
    });
    setSources((src?.sources ?? r.review?.sources ?? {}) as TermSources);
  }

  useEffect(() => {
    getWording(id).then(seed).catch(e => setErr(fieldErrors(e).message));
    getTreatyTypes().then(setTypes).catch(() => setTypes([]));
  }, [id]);

  const pages = rec?.wording?.validation.pages ?? 0;
  useEffect(() => {
    if (!rec?.wording || !pages) return;
    let url: string | null = null;
    setImg(null);
    wordingPageUrl(id, page).then(u => { url = u; setImg(u); }).catch(() => setImg(null));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [id, page, rec?.wording, pages]);

  const spec = useMemo(() => types.find(t => t.key === draft?.treaty_type) ?? null,
                       [types, draft?.treaty_type]);
  const unconfirmed = !!rec?.reading && (!rec.review || rec.reading.read_at > rec.review.confirmed_at);

  async function doRead() {
    setBusy("read"); setErr(""); setNote("");
    try {
      const r = await readWording(id);
      seed(r);
      const n = Object.keys(r.reading?.sources ?? {}).length;
      setNote(`The wording was read — ${n} term${n === 1 ? "" : "s"} traced to their page. `
        + "Check each one against the document, correct anything, then confirm.");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  async function doConfirm() {
    if (!draft) return;
    setBusy("confirm"); setErr(""); setNote("");
    try {
      const r = await confirmWording(id, {
        treaty: draft, sources, effective_date: dates.effective_date || null,
        inception_dt: dates.inception_dt || null, expiry_dt: dates.expiry_dt || null,
        year_of_account: dates.year_of_account || null,
        notice_period_days: dates.notice_period_days ? Number(dates.notice_period_days) : null,
      });
      seed(r);
      setNote("Terms confirmed from the signed wording — the treaty is configured from them."
        + (r.notes?.length ? ` ${r.notes.join(" ")}` : ""));
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(fe.message + (Object.keys(fe.errors).length
        ? ` ${Object.entries(fe.errors).map(([k, v]) => `${k}: ${v}`).join("; ")}` : ""));
    } finally {
      setBusy("");
    }
  }

  /** The page-and-quote chip beside a term (C). */
  const Src = ({ path }: { path: string }) => {
    const s = sources[path];
    if (!s) return <span className="sub" style={{ fontSize: 11 }} title="Not found in the wording">—</span>;
    return (
      <button type="button" className="badge b-info" style={{ border: 0, cursor: "pointer", whiteSpace: "nowrap" }}
              title={s.quote ? `“${s.quote}”` : `Page ${s.page}`}
              onClick={() => { setPage(s.page); setQuote({ path, page: s.page, quote: s.quote }); }}>
        <span className="d" />p. {s.page}
      </button>
    );
  };

  const setTerm = (k: string, v: unknown) =>
    setDraft(d => d && { ...d, terms: { ...d.terms, [k]: v } });
  const setLayerTerm = (i: number, k: string, v: unknown) =>
    setDraft(d => d && { ...d, layers: d.layers.map((l, j) => j === i ? { ...l, terms: { ...l.terms, [k]: v } } : l) });

  function visible(f: TreatyField, terms: Record<string, unknown>) {
    return !f.when || String(terms[f.when.field] ?? "") === f.when.equals;
  }

  // Render functions, not components: a component defined in here would be a
  // new type every render, and its input would lose focus on each keystroke.
  const input = (f: TreatyField, value: unknown, onChange: (v: unknown) => void) =>
    f.kind === "select" ? (
      <select value={String(value ?? "")} onChange={e => onChange(e.target.value || null)}>
        <option value="">—</option>
        {(f.options ?? []).map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    ) : (
      <input value={value == null ? "" : String(value)} placeholder={f.example ?? undefined}
             inputMode={f.kind === "text" ? undefined : "decimal"}
             onChange={e => onChange(f.kind === "text" || e.target.value === "" ? (e.target.value || null)
               : Number(e.target.value.replace(/,/g, "")))} />
    );

  const termRow = (f: TreatyField, value: unknown, path: string,
                   onChange: (v: unknown) => void, terms: Record<string, unknown>) => {
    if (!visible(f, terms)) return null;
    const missing = f.required && (value == null || value === "");
    return (
      <tr key={path}>
        <td style={{ width: "42%" }}>
          <b style={{ fontWeight: 600 }}>{f.label}</b>
          {f.required && <span style={{ color: "var(--p-crit-ink)" }}> *</span>}
          {f.hint && <div className="sub" style={{ fontSize: 11.5 }}>{f.hint}</div>}
        </td>
        <td style={missing ? { boxShadow: "inset 2px 0 0 var(--p-warn)" } : undefined}>
          {f.kind === "sliding" || f.kind === "profit" ? (
            // A commission term is its own small structure, not one number.
            <OptionalCommission kind={f.kind} label={f.label}
              value={value as SlidingScale | ProfitCommission | null | undefined}
              onChange={onChange} />
          ) : input(f, value, onChange)}
        </td>
        <td style={{ width: 70, textAlign: "center" }}><Src path={path} /></td>
      </tr>
    );
  };

  if (!rec) {
    return <div className="proto"><div className="view full">
      {err ? <div className="note warn">{err}</div> : <div className="sub">Loading…</div>}
    </div></div>;
  }
  const w = rec.wording;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Review Wording &amp; Terms</h2>
            <p>{rec.treaty} · configured from its signed wording</p>
            <div className="about-strip">
              <span className="chip"><span className="k">Treaty</span>{rec.lifecycle}</span>
              <span className="chip"><span className="k">Documentation</span>{rec.documentation.label}</span>
              <span className="chip"><span className="k">Configuration</span>{rec.configuration.label}</span>
            </div>
          </div>
          <div className="actions">
            <Link className="btn" to={`/contracts/${id}`}><ArrowLeft size={14} /> Back to treaty</Link>
            {w && w.validation.status === "valid" && (
              <button className="btn" type="button" disabled={!!busy} onClick={doRead}>
                {busy === "read" ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
                {" "}{rec.reading ? "Read the wording again" : "Read the wording"}
              </button>
            )}
            {w && (
              <button className="btn pri" type="button" disabled={!!busy || !draft?.treaty_type}
                      onClick={doConfirm}>
                {busy === "confirm" ? <Loader2 size={14} className="animate-spin" /> : <CheckCircle2 size={14} />}
                {" "}Confirm terms
              </button>
            )}
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
        {note && <div className="note ok" style={{ marginBottom: 14 }}>{note}</div>}
        {!w && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            <b>Signed treaty wording required</b>
            <div>Upload the signed treaty wording before starting treaty configuration.</div>
            <Link className="btn sm pri" style={{ marginTop: 8 }} to={`/contracts/${id}`}>Upload Wording</Link>
          </div>
        )}
        {w && rec.review && !unconfirmed && (
          <div className="note ok" style={{ marginBottom: 14 }}>
            <b>Terms confirmed</b> by {rec.review.confirmed_by ?? "—"} on {fmtStamp(rec.review.confirmed_at)},
            from {rec.review.document.filename}{rec.review.document.version ? ` (${rec.review.document.version})` : ""}.
          </div>
        )}
        {w && unconfirmed && (
          <div className="note" style={{ marginBottom: 14 }}>
            <b>Read {fmtStamp(rec.reading!.read_at)} — not confirmed yet.</b> Each term shows the page it was
            read from; click it to see the words in the document.
            {!!rec.reading!.unmatched_reinsurers.length && (
              <> Reinsurers named in the wording but not on file:{" "}
                <b>{rec.reading!.unmatched_reinsurers.join(", ")}</b> — add them on Treaty structure.</>
            )}
          </div>
        )}

        {w && (
          <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr)", gap: 18 }}
               className="wording-review">
            {/* ── A. the source document ── */}
            <div className="card" style={{ alignSelf: "start", position: "sticky", top: 12 }}>
              <div className="card-h" style={{ flexWrap: "wrap", gap: 8 }}>
                <FileText size={16} className="ci" />
                <h3 style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{w.filename}</h3>
                <span className="right" style={{ display: "flex", gap: 6 }}>
                  {w.version && <span className="tag-pill">{w.version}</span>}
                  <span className="badge b-ok"><span className="d" />
                    {w.source === "esigned" ? "Signed in Kavachio" : "Signed copy"}</span>
                </span>
              </div>
              <div style={{ padding: "10px 20px", display: "flex", gap: 16, flexWrap: "wrap" }} className="sub">
                <span>{w.validation.label}</span>
                {w.uploaded_at && <span>Uploaded {fmtStamp(w.uploaded_at)}{w.uploaded_by ? ` by ${w.uploaded_by}` : ""}</span>}
                <span>Effective {dates.effective_date ? fmtDate(dates.effective_date)
                  : dates.inception_dt ? `${fmtDate(dates.inception_dt)} (inception)` : "— not identified"}</span>
              </div>
              {quote && quote.page === page && (
                <div className="note" style={{ margin: "0 20px 10px" }}>
                  <b>Page {quote.page}</b>{quote.quote ? <> — &ldquo;{quote.quote}&rdquo;</> : null}
                  <span className="sub"> · {quote.path}</span>
                </div>
              )}
              <div style={{ padding: "0 20px", background: "var(--p-surface-2)", minHeight: 420,
                            display: "grid", placeItems: "center" }}>
                {img ? <img src={img} alt={`Page ${page}`} style={{ width: "100%", display: "block",
                         border: "1px solid var(--p-border)", margin: "12px 0" }} />
                     : <span className="sub"><Loader2 size={14} className="animate-spin" /> Loading page…</span>}
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "10px 20px" }}>
                <button className="btn sm" type="button" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>
                  <ChevronLeft size={13} /> Previous</button>
                <span className="sub" style={{ flex: 1, textAlign: "center" }}>Page {page} of {pages || "—"}</span>
                <button className="btn sm" type="button" disabled={page >= pages} onClick={() => setPage(p => p + 1)}>
                  Next <ChevronRight size={13} /></button>
              </div>
            </div>

            {/* ── B + C. the terms, each traced to its page ── */}
            <div className="card" style={{ alignSelf: "start" }}>
              <div className="card-h">
                <h3>Contractual terms</h3>
                <span className="sub">
                  {unconfirmed ? "as read from the wording — check and correct"
                    : rec.review ? "as confirmed" : "as recorded — read the wording to trace them"}
                </span>
              </div>
              {!draft ? (
                <div className="empty" style={{ padding: 20 }}>
                  Nothing read yet. Press <b>Read the wording</b> to extract its terms, each traced to its page
                  {w.validation.status !== "valid" && " — or, as this wording has no readable text, set them on Treaty structure"}.
                </div>
              ) : (
                <div className="tbl-wrap">
                  <table className="panel-tbl" style={{ textAlign: "left" }}>
                    <thead><tr><th>Term</th><th>Value</th><th style={{ textAlign: "center" }}>Source</th></tr></thead>
                    <tbody>
                      <tr>
                        <td><b style={{ fontWeight: 600 }}>Treaty type</b><span style={{ color: "var(--p-crit-ink)" }}> *</span></td>
                        <td>
                          <select value={draft.treaty_type ?? ""} onChange={e =>
                            setDraft(d => d && { ...d, treaty_type: (e.target.value || null) as TreatyInput["treaty_type"] })}>
                            <option value="">—</option>
                            {types.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
                          </select>
                        </td>
                        <td style={{ textAlign: "center" }}><Src path="treaty_type" /></td>
                      </tr>
                      {([["inception_dt", "Inception", "date"], ["expiry_dt", "Expiry", "date"],
                         ["effective_date", "Effective date", "date"],
                         ["year_of_account", "Year of account", "text"],
                         ["notice_period_days", "Notice of cancellation (days)", "text"]] as const).map(([k, label, kind]) => (
                        <tr key={k}>
                          <td><b style={{ fontWeight: 600 }}>{label}</b>
                            {(k === "inception_dt" || k === "expiry_dt") && !rec.editable_dates && (
                              <div className="sub" style={{ fontSize: 11.5 }}>Signed — corrected with Change period</div>)}</td>
                          <td><input type={kind} value={dates[k]}
                                     disabled={(k === "inception_dt" || k === "expiry_dt") && !rec.editable_dates}
                                     onChange={e => setDates(d => ({ ...d, [k]: e.target.value }))} /></td>
                          <td style={{ textAlign: "center" }}><Src path={k} /></td>
                        </tr>
                      ))}
                      {spec && <tr><td colSpan={3} className="sub" style={{ fontWeight: 600, paddingTop: 16 }}>
                        {spec.label} terms</td></tr>}
                      {spec?.fields.map(f => (
                        termRow(f, draft.terms[f.name], `terms.${f.name}`, v => setTerm(f.name, v), draft.terms)
                      ))}
                      {spec && draft.layers.map((l, i) => (
                        <LayerRows key={i} i={i} name={l.name} n={draft.layers.length}>
                          {spec.layer_fields.map(f => (
                            termRow(f, l.terms[f.name], `layers.${i}.terms.${f.name}`,
                                    v => setLayerTerm(i, f.name, v), l.terms)
                          ))}
                          {l.shares.map((sh, j) => (
                            <tr key={`s${j}`}>
                              <td>
                                <b style={{ fontWeight: 600 }}>{sh.reinsurer_name ?? `Reinsurer ${sh.reinsurer_party_id ?? "?"}`}</b>
                                {sh.is_lead && <span className="badge b-info" style={{ marginLeft: 6 }}>Lead</span>}
                                {!sh.reinsurer_party_id && <div className="sub" style={{ color: "var(--p-warn-ink)", fontSize: 11.5 }}>
                                  Not on file — add the reinsurer on Treaty structure</div>}
                              </td>
                              <td>
                                <input inputMode="decimal" value={sh.share_pct == null ? "" : String(sh.share_pct)}
                                       onChange={e => setDraft(d => d && { ...d, layers: d.layers.map((ll, ii) => ii !== i ? ll : {
                                         ...ll, shares: ll.shares.map((ss, jj) => jj !== j ? ss : {
                                           ...ss, share_pct: e.target.value === "" ? null : Number(e.target.value) }) }) })} />
                                <span className="sub"> % of the layer</span>
                              </td>
                              <td style={{ textAlign: "center" }}><Src path={`layers.${i}.shares.${j}`} /></td>
                            </tr>
                          ))}
                        </LayerRows>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function LayerRows({ i, name, n, children }: { i: number; name: string; n: number; children: React.ReactNode }) {
  return (
    <>
      <tr><td colSpan={3} className="sub" style={{ fontWeight: 600, paddingTop: 16 }}>
        {n > 1 ? `Layer ${i + 1} — ${name}` : name || "Layer"}</td></tr>
      {children}
    </>
  );
}
