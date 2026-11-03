/**
 * Mid-term change — a running contract's terms and clauses change from a date.
 *
 * The contract stays the same contract. The carrier says from when, why, and
 * what moves — its terms (pre-filled with what they are today) and its clauses
 * (re-worded, added or deleted, on the clauses as they read today). Everything
 * else on this page is the server's preview of what that does
 * (POST /contracts/{id}/mid-term/preview), so the screen never decides which
 * checks change:
 *
 *  · which day is old and which is new — said in words, so nobody has to work
 *    out which side the 15th falls on;
 *  · each check that changes, as "until 15 Aug 2026 → from 16 Aug 2026", the
 *    checks a re-worded or new clause makes (worked out by Bordereau Setup's
 *    own rule generation, on those clauses only), and how many are left
 *    exactly as they are;
 *  · the column each Bordereau Setup dates a row by (its transaction effective
 *    date) — required, because a dated check cannot be placed without it;
 *  · files already checked that will be listed for a re-check once it is live.
 *
 * A signed change document can be uploaded instead of typing: Kavachio reads
 * what it changes and fills the page in; signed by both sides, it is accepted
 * and goes live with no broker round. Otherwise sending changes nothing yet:
 * the broker agrees, both sides sign, and only then does it go live.
 * `?revise=1` re-opens the change in progress, starting from what was sent.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, FileText, Plus, Send, Trash2, Undo2, Upload } from "lucide-react";
import {
  fieldErrors, getContractTypes, getMidTerm, previewMidTerm, reviseMidTerm, sendMidTerm,
  uploadMidTermDocument,
  type AgreedLimitSpec, type AgreedLimits, type LimitGroup, type MidTermClause,
  type MidTermClauseEdit, type MidTermInput, type MidTermPreview, type MidTermStart,
  type SeveritySpec,
} from "../api/contractRecord";
import { InfoTip } from "../components/InfoTip";
import { WordingEditor } from "../components/WordingEditor";
import { fmtDate } from "../utils/date";
import { wordingTokenLabels } from "../utils/wordingTokens";

function plain(terms: AgreedLimits | null | undefined): AgreedLimits {
  const out: AgreedLimits = {};
  for (const [k, v] of Object.entries(terms ?? {})) {
    out[k] = { value: v.value, ...(v.severity ? { severity: v.severity } : {}) };
  }
  return out;
}

/** An edit to one of the contract's clauses, keyed by its ref. */
type ClauseEdit = { action: "amended" | "removed"; title: string; text: string };
/** A clause the change adds. `key` only tells the rows apart on screen. */
type NewClause = { key: string; title: string; section: string; text: string };

const SHOWN = 4;   // lines of a clause shown before "more"

type TabKey = "document" | "when" | "terms" | "clauses" | "checks" | "setup" | "files" | "preview";

/** The tab each of the server's blockers (mid_term_routes._plan) is settled on.
 *  A code not listed here is not settled on this page. */
const BLOCKER_TAB: Record<string, TabKey> = {
  effective_from: "when", reason: "when",
  no_change: "terms",
  clauses: "clauses",
  needs_choice: "checks", clause_checks: "checks",
  date_column: "setup",
  document: "document",
};

const TAB_COUNT: React.CSSProperties = {
  fontSize: 11, fontWeight: 700, lineHeight: "16px", minWidth: 18, padding: "0 6px",
  borderRadius: 9, textAlign: "center", background: "var(--p-primary-soft)",
  color: "var(--p-primary)",
};

export default function MidTermChange() {
  const { contractId } = useParams();
  const id = Number(contractId);
  const [sp] = useSearchParams();
  const revising = sp.get("revise") === "1";
  const nav = useNavigate();

  const [start, setStart] = useState<MidTermStart | null>(null);
  const [limitSpec, setLimitSpec] = useState<AgreedLimitSpec[]>([]);
  const [groups, setGroups] = useState<LimitGroup[]>([]);
  const [sevSpec, setSevSpec] = useState<SeveritySpec[]>([]);
  const [limits, setLimits] = useState<AgreedLimits>({});
  const [from, setFrom] = useState("");
  const [reason, setReason] = useState("");
  const [dateCols, setDateCols] = useState<Record<string, string>>({});
  const [choices, setChoices] = useState<Record<string, number[]>>({});
  const [edits, setEdits] = useState<Record<string, ClauseEdit>>({});
  const [added, setAdded] = useState<NewClause[]>([]);
  const [doc, setDoc] = useState<{ id: number; filename: string } | null>(null);
  const [signed, setSigned] = useState(true);
  const [docNote, setDocNote] = useState("");
  const [workedOutFor, setWorkedOutFor] = useState<string | null>(null);
  const [pv, setPv] = useState<MidTermPreview | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [find, setFind] = useState("");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState("");
  // One section at a time: the page held eight cards one under another, and
  // every step meant scrolling past the rest to reach it (or the Send button).
  const [tab, setTab] = useState<TabKey>(revising ? "when" : "document");

  useEffect(() => {
    let live = true;
    Promise.all([getContractTypes(), getMidTerm(id)])
      .then(([types, st]) => {
        if (!live) return;
        setLimitSpec(types.agreed_limits);
        setGroups(types.limit_groups);
        setSevSpec(types.severities);
        setStart(st);
        const req = revising ? st.open?.request : null;
        setLimits(plain(req?.agreed_limits ?? st.current_terms));
        if (req) {
          setFrom(req.effective_from ?? "");
          setReason(req.reason ?? "");
          setChoices(req.choices ?? {});
          const byRef: Record<string, ClauseEdit> = {};
          const fresh: NewClause[] = [];
          (req.clauses ?? []).forEach((e, i) => {
            if (e.action === "added") {
              fresh.push({ key: `n${i}`, title: e.title ?? "", section: e.section ?? "",
                           text: e.text ?? "" });
            } else if (e.ref) {
              const cur = st.clauses?.find(c => c.ref === e.ref);
              byRef[e.ref] = { action: e.action, title: e.title ?? cur?.title ?? "",
                               text: e.text ?? (cur?.body ?? cur?.text ?? "") };
            }
          });
          setEdits(byRef);
          setAdded(fresh);
        }
        const cols: Record<string, string> = {};
        for (const s of st.setups ?? []) {
          const c = req?.date_columns?.[String(s.pipeline_id)] ?? s.date_column;
          if (c) cols[String(s.pipeline_id)] = c;
        }
        setDateCols(cols);
      })
      .catch(e => live && setMsg(fieldErrors(e).message));
    return () => { live = false; };
  }, [id, revising]);

  const authored = !!start && !start.contract.is_uploaded;
  const clauses = useMemo(() => start?.clauses ?? [], [start]);
  const tokenLabels = useMemo(() => wordingTokenLabels(limitSpec), [limitSpec]);

  // What the server is sent for the clauses: an edit per changed clause, and
  // each new one. An edit made back to what the clause said drops out there.
  const clauseEdits: MidTermClauseEdit[] = useMemo(() => [
    ...Object.entries(edits).map(([ref, e]) => (
      e.action === "removed" ? { ref, action: "removed" as const }
        : { ref, action: "amended" as const, title: e.title, text: e.text })),
    ...added.map(a => ({ action: "added" as const, title: a.title, section: a.section,
                         text: a.text })),
  ], [edits, added]);
  const clausesKey = useMemo(() => JSON.stringify(clauseEdits), [clauseEdits]);

  const body: MidTermInput = useMemo(() => ({
    effective_from: from || null, reason: reason || null, agreed_limits: limits,
    choices, date_columns: dateCols, clauses: clauseEdits,
    document_id: doc?.id ?? null, signed: !!doc && signed,
    // the checks a re-worded clause makes are asked for once, on request; the
    // answer is kept (and cached) until the clauses change again
    work_out_checks: workedOutFor === clausesKey,
  }), [from, reason, limits, choices, dateCols, clauseEdits, doc, signed, workedOutFor,
       clausesKey]);

  const refresh = useCallback(async () => {
    try { setPv(await previewMidTerm(id, body)); setMsg(""); }
    catch (e) { setMsg(fieldErrors(e).message); }
  }, [id, body]);

  useEffect(() => {
    if (!start) return;
    const t = window.setTimeout(refresh, 400);
    return () => window.clearTimeout(t);
  }, [start, refresh]);

  async function workOut() {
    setBusy("checks");
    setMsg("");
    try {
      setPv(await previewMidTerm(id, { ...body, work_out_checks: true }));
      setWorkedOutFor(clausesKey);
    } catch (e) { setMsg(fieldErrors(e).message); }
    finally { setBusy(""); }
  }

  const current = start?.current_terms ?? {};
  const changedKeys = new Set((pv?.changes ?? []).map(c => c.key));

  function setLimit(key: string, patch: Partial<AgreedLimits[string]>) {
    setLimits(l => {
      const next = { ...(l[key] ?? { value: "" }), ...patch };
      if (next.value === "" || next.value === null) {
        const { [key]: _drop, ...rest } = l;
        return rest;
      }
      return { ...l, [key]: next };
    });
  }

  function editClause(c: MidTermClause) {
    setEdits(e => ({ ...e, [c.ref]: { action: "amended", title: c.title ?? "",
                                      text: c.body ?? c.text } }));
  }
  function undoClause(ref: string) {
    setEdits(e => { const { [ref]: _drop, ...rest } = e; return rest; });
  }

  async function pickDocument(file: File | undefined) {
    if (!file) return;
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
      setDocNote("Upload the change document as a PDF.");
      return;
    }
    setBusy("doc");
    setDocNote("");
    try {
      const r = await uploadMidTermDocument(id, file);
      setDoc(r.document);
      setSigned(true);
      const read = r.read as MidTermStartRead;
      if (r.error || !read || !("clauses" in read)) {
        setDocNote(r.error ?? "Kavachio couldn't read what this document changes — fill it in below.");
        return;
      }
      if (read.effective_from && !from) setFrom(read.effective_from);
      if (read.reason && !reason) setReason(read.reason);
      const moved = Object.entries(read.terms ?? {});
      if (moved.length) {
        setLimits(l => {
          const next = { ...l };
          moved.forEach(([k, v]) => { next[k] = { ...(next[k] ?? {}), value: v }; });
          return next;
        });
      }
      const byRef: Record<string, ClauseEdit> = {};
      const fresh: NewClause[] = [];
      (read.clauses ?? []).forEach((e, i) => {
        if (e.action === "added") {
          fresh.push({ key: `d${Date.now()}_${i}`, title: e.title ?? "", section: e.section ?? "",
                       text: e.text ?? "" });
        } else if (e.ref) {
          const cur = clauses.find(c => c.ref === e.ref);
          byRef[e.ref] = { action: e.action, title: e.title ?? cur?.title ?? "",
                           text: e.text ?? (cur?.body ?? cur?.text ?? "") };
        }
      });
      setEdits(e => ({ ...e, ...byRef }));
      setAdded(a => [...a, ...fresh]);
      setDocNote(`Read from ${r.document.filename}: `
        + [read.effective_from ? `from ${fmtDate(read.effective_from)}` : null,
           moved.length ? `${moved.length} term${moved.length === 1 ? "" : "s"}` : null,
           Object.keys(byRef).length ? `${Object.keys(byRef).length} clause${Object.keys(byRef).length === 1 ? "" : "s"} changed` : null,
           fresh.length ? `${fresh.length} new clause${fresh.length === 1 ? "" : "s"}` : null]
          .filter(Boolean).join(" · ")
        + ". Check everything below before you go on.");
    } catch (e) {
      setDocNote(fieldErrors(e).message);
    } finally { setBusy(""); }
  }

  async function send() {
    if (!pv?.ok) return;
    setBusy("send");
    setMsg("");
    try {
      const openChange = start?.open;
      const rec = revising && openChange
        ? await reviseMidTerm(id, openChange.id, body)
        : await sendMidTerm(id, body);
      const live = rec.mid_term?.history?.find(h => h.status === "live"
                                               && h.number === pv.number);
      const pending = rec.mid_term?.open;
      const notice = pv.signed
        ? (live ? `Mid-term change ${pv.number} accepted — live from ${fmtDate(live.effective_from)}.`
                + " Files are checked on the new terms from that date."
          : `Mid-term change ${pending?.number ?? pv.number} is waiting for the carrier admin to accept it.`)
        : `Mid-term change ${pending?.number ?? ""} sent to ${rec.counterparty?.name ?? "the broker"} for `
          + "agreement. Nothing changes yet — files are checked on the current terms "
          + "until it is agreed and signed by both sides.";
      nav(`/contracts/${id}`, { state: { notice } });
    } catch (e) {
      setMsg(fieldErrors(e).message);
    } finally { setBusy(""); }
  }

  if (!start) {
    return (
      <div className="proto"><div className="view full">
        <div className="page-head"><div className="t"><h2>Mid-term change</h2></div></div>
        {msg ? <div className="note warn">{msg}</div>
          : <div className="empty">Loading the contract and reading its current terms…</div>}
      </div></div>
    );
  }

  const ct = start.contract;
  const blocked = revising ? !start.open : !start.can_start;
  const visible = limitSpec.filter(l =>
    showAll || l.common || current[l.name] !== undefined || limits[l.name] !== undefined);
  const unit = (l: AgreedLimitSpec) => l.unit
    || (l.kind === "money" ? String(limits.currency?.value ?? current.currency?.value ?? "") : "");
  const show = (l: AgreedLimitSpec, v: unknown) =>
    v === undefined || v === null || v === "" ? "—" : `${v}${unit(l) ? " " + unit(l) : ""}`;
  const clauseTitle = (cid?: number | null) =>
    cid ? clauses.find(c => c.clause_id === cid)?.title ?? null : null;

  const r = pv?.rules;
  const cc = pv?.clause_checks;
  const closingById = new Map((r?.closing ?? []).map(c => [c.rule_id, c]));
  const replaced = new Set((r?.opening ?? []).map(o => o.replaces).filter(Boolean));
  const autoRefs = new Set((pv?.clause_changes ?? []).filter(c => c.auto).map(c => c.ref));
  const editedCount = Object.keys(edits).length + added.length;

  // The clauses, grouped under their section heading as the contract has them.
  const needle = find.trim().toLowerCase();
  const shown = clauses.filter(c => !needle || `${c.title ?? ""} ${c.section ?? ""} ${c.text}`
    .toLowerCase().includes(needle) || edits[c.ref]);
  const sections: { name: string; items: MidTermClause[] }[] = [];
  shown.forEach(c => {
    const name = authored ? "" : (c.section ?? "");
    const last = sections[sections.length - 1];
    if (last && last.name === name) last.items.push(c);
    else sections.push({ name, items: [c] });
  });
  const sectionNames = Array.from(new Set(clauses.map(c => c.section).filter(Boolean))) as string[];

  // The tabs. Each carries what it is waiting on: a count of what changed in
  // it, or a dot when something there still stops the change being sent — the
  // same blockers listed by the Send button, each of which opens its tab.
  const setupsShown = (pv?.setups ?? start.setups ?? []).length > 0;
  const blockersIn = (k: TabKey) => (pv?.blockers ?? []).filter(b => BLOCKER_TAB[b.code] === k).length;
  const tabs = ([
    { key: "document", label: "Upload Document", show: !revising, count: doc ? 1 : 0 },
    { key: "when", label: "Start Date & Reason", show: true, count: 0 },
    { key: "terms", label: "Changed Terms", show: true, count: pv?.changes.length ?? 0 },
    { key: "clauses", label: "Changed Wording", show: true, count: editedCount },
    { key: "checks", label: "Rule Changes", show: true,
      count: (r?.opening.length ?? 0) + (cc?.opening?.length ?? 0)
        + (r?.closing ?? []).filter(c => !replaced.has(c.rule_id)).length },
    { key: "setup", label: "Date Column", show: setupsShown, count: 0 },
    { key: "files", label: "Files to Recheck", show: !!pv?.recheck?.length,
      count: pv?.recheck?.length ?? 0 },
    { key: "preview", label: "Preview", show: !!pv?.document || !!pv?.sections?.length,
      count: 0 },
  ] as { key: TabKey; label: string; show: boolean; count: number }[]).filter(t => t.show);
  const curTab: TabKey = tabs.some(t => t.key === tab) ? tab : tabs[0].key;

  return (
    <div className="proto"><div className="view full">
      <div className="page-head">
        <div className="t">
          <h2>{revising ? `Revise mid-term change ${start.open?.number ?? ""}` : "Mid-term change"}</h2>
          <p>
            {[ct.name, ct.counterparty, ct.programme].filter(Boolean).join(" · ")}
            {ct.inception_dt && ` · ${fmtDate(ct.inception_dt)} – ${fmtDate(ct.expiry_dt)}`}
          </p>
        </div>
        <div className="actions">
          <Link className="btn" to={`/contracts/${id}`}><ArrowLeft size={13} /> Back to the contract</Link>
        </div>
      </div>

      {blocked && (
        <div className="note warn">
          {revising ? "There is no mid-term change in progress to revise."
            : (start.why_not ?? "A mid-term change can't be made on this contract now.")}
        </div>
      )}
      {start.open?.change_request && revising && (
        <div className="note" style={{ marginBottom: 14 }}>
          <b>The broker asked:</b> {start.open.change_request}
        </div>
      )}

      {!blocked && (<>
        <div className="tabs" role="tablist" style={{ overflowX: "auto" }}>
          {tabs.map(t => {
            const waiting = blockersIn(t.key);
            return (
              <button key={t.key} type="button" role="tab" aria-selected={curTab === t.key}
                      className={curTab === t.key ? "on" : ""} onClick={() => setTab(t.key)}
                      style={{ whiteSpace: "nowrap", display: "inline-flex", alignItems: "center", gap: 6 }}>
                {t.label}
                {t.count > 0 && <span style={TAB_COUNT}>{t.count}</span>}
                {waiting > 0 && (
                  <span title={`${waiting} thing${waiting === 1 ? "" : "s"} to settle here before sending`}
                        style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--p-warn)" }} />
                )}
              </button>
            );
          })}
        </div>

        {/* 0. A signed change document, if there is one */}
        {curTab === "document" && (
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-h">
              <h3>Change document</h3>
              <InfoTip text="Already have the change document? Upload it and Kavachio reads what
                it changes — the date, the terms and the clauses — and fills in this page for you
                to check. If it is signed by both sides it is accepted and goes live with no
                broker round. Without one, Kavachio writes the change document from what you
                change below, and the broker agrees and both sides sign it." />
            </div>
            <div style={{ padding: "4px 18px 16px", fontSize: 13 }}>
              {doc ? (
                <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                    <FileText size={13} /> <b>{doc.filename}</b>
                  </span>
                  <label style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <input type="checkbox" checked={signed} onChange={e => setSigned(e.target.checked)} />
                    Signed by both sides — accept it and make it live (no broker round)
                  </label>
                  <span className="linkish" role="button"
                        onClick={() => { setDoc(null); setDocNote(""); }}>Remove</span>
                </div>
              ) : (
                <label className="btn" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <Upload size={13} /> {busy === "doc" ? "Reading the document…" : "Upload the change document (PDF)"}
                  <input type="file" accept=".pdf,application/pdf" style={{ display: "none" }}
                         disabled={!!busy}
                         onChange={e => { void pickDocument(e.target.files?.[0]); e.target.value = ""; }} />
                </label>
              )}
              {!doc && <span className="hint" style={{ marginLeft: 10 }}>Optional — or make the change below.</span>}
              {docNote && <div className="note" style={{ marginTop: 10 }}>{docNote}</div>}
            </div>
          </div>
        )}

        {/* 1. When, and why */}
        {curTab === "when" && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>From when, and why</h3>
          </div>
          <div style={{ padding: "4px 18px 16px" }}>
            <div className="grid g-2">
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Takes effect from</label>
                <input type="date" value={from} onChange={e => setFrom(e.target.value)}
                       min={ct.inception_dt ?? undefined} max={ct.expiry_dt ?? undefined} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Reason <span className="muted" style={{ fontWeight: 500 }}>— the broker sees this</span></label>
                <input value={reason} placeholder="e.g. Commission agreed at the mid-year review"
                       onChange={e => setReason(e.target.value)} />
              </div>
            </div>
            {pv?.boundary && (
              <div className="note" style={{ marginTop: 12 }}>
                <b>Up to and including {pv.boundary.old_until}</b>: current terms.{" "}
                <b>From {pv.boundary.new_from}</b>: new terms.{" "}
                <InfoTip text={pv.boundary.sentence + " A row's date is its transaction "
                  + "effective date — the column chosen for each setup below — not the "
                  + "policy's inception date."} />
              </div>
            )}
          </div>
        </div>

        )}

        {/* 2. The terms */}
        {curTab === "terms" && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>What changes — terms</h3>
            <InfoTip text={ct.is_uploaded
              ? "This contract was uploaded, so its terms are read from what it says: from the checks it runs today and, for the rest, from its clauses. Each box starts at today's value — change the ones that move. A term read from a clause re-words that clause too."
              : "Each box starts at the term as it is today — change the ones that move. The wording quoting a term follows it."} />
          </div>
          <div style={{ padding: "4px 18px 16px" }}>
            {start.terms_error && (
              <div className="note warn" style={{ marginBottom: 10 }}>
                {start.terms_error} Some current values may be missing — fill in what moves.
              </div>
            )}
            {groups.map(g => {
              const rows = visible.filter(l => l.group === g.key);
              if (!rows.length) return null;
              return (
                <div className="limgrp" key={g.key} style={{ marginBottom: 12 }}>
                  <div className="limgrp-h"><div className="sub-h">{g.label}</div></div>
                  <div className="limgrp-b">
                    <div className="lim lim-h">
                      <div className="lq"><b>Term</b></div>
                      <div className="sub">Now → from {pv?.boundary?.new_from ?? "the change date"}</div>
                      <div className="sub">Severity</div>
                    </div>
                    {rows.map(l => {
                      const entry = limits[l.name];
                      const now = current[l.name];
                      const changed = changedKeys.has(l.name);
                      const sev = entry?.severity ?? l.default_severity;
                      const fromClause = clauseTitle(now?.clause_id);
                      return (
                        <div className="lim" key={l.name}
                             style={changed ? { background: "var(--p-warn-soft, #FFF7E6)" } : undefined}>
                          <div className="lq">
                            <b>{l.question}</b>
                            <span>
                              Now: {show(l, now?.value)}
                              {fromClause && <> · <span title={now?.quote ?? undefined}>from “{fromClause}”</span></>}
                            </span>
                          </div>
                          <div className={l.kind === "choice" ? "" : "unit"}>
                            {/* A list ("New, Renewal, …") is not one of the
                                choices, so it is edited as text, not lost. */}
                            {l.choices && (entry?.value === undefined || entry?.value === ""
                                           || l.choices.includes(String(entry.value))) ? (
                              <select value={String(entry?.value ?? "")}
                                      onChange={e => setLimit(l.name, { value: e.target.value })}>
                                <option value="">Not agreed</option>
                                {l.choices.map(c => <option key={c} value={c}>{c}</option>)}
                              </select>
                            ) : (<>
                              <input value={String(entry?.value ?? "")} placeholder="—"
                                     onChange={e => setLimit(l.name, { value: e.target.value })} />
                              {unit(l) && <em>{unit(l)}</em>}
                            </>)}
                          </div>
                          <div>
                            {entry === undefined ? <span className="sub">—</span>
                              : l.checkable ? (
                                <div className="segpick">
                                  {sevSpec.map(sv => (
                                    <button key={sv.key} type="button" title={sv.hint}
                                            className={sev === sv.key ? "on" : ""}
                                            onClick={() => setLimit(l.name, { severity: sv.key })}>
                                      {sv.label}
                                    </button>
                                  ))}
                                </div>
                              ) : <span className="sub">Wording only — nothing in a file to check.</span>}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
            <span className="linkish" role="button" onClick={() => setShowAll(v => !v)}>
              {showAll ? "Show fewer terms" : "Show every term"}
            </span>
          </div>
        </div>

        )}

        {/* 3. The clauses */}
        {curTab === "clauses" && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>What changes — clauses</h3>
            {editedCount > 0 && <span className="badge b-info"><span className="d" />{editedCount} changed</span>}
            <InfoTip text="The contract's clauses as they read today. Re-word one, delete one, or add a
              new one. The checks written from a re-worded or deleted clause end the day before the
              change, and the checks its new words make run from the change date — every other
              clause and check stays exactly as it is." />
          </div>
          <div style={{ padding: "4px 18px 16px", fontSize: 13 }}>
            {clauses.length > 8 && (
              <div className="field" style={{ maxWidth: 360, marginBottom: 10 }}>
                <input value={find} placeholder="Find a clause…" onChange={e => setFind(e.target.value)} />
              </div>
            )}
            {!clauses.length && <div className="hint">This contract has no clauses on file.</div>}
            <div style={{ maxHeight: 620, overflowY: "auto", paddingRight: 4 }}>
              {sections.map((sec, si) => (
                <div key={`${sec.name}-${si}`} style={{ marginBottom: 10 }}>
                  {sec.name && <div className="sub-h" style={{ margin: "8px 0 4px" }}>{sec.name}</div>}
                  {sec.items.map(c => {
                    const e = edits[c.ref];
                    const lines = (c.text || "").split("\n");
                    const long = lines.length > SHOWN || c.text.length > 420;
                    const expanded = !!open[c.ref];
                    return (
                      <div key={c.ref} style={{
                        border: "1px solid var(--p-line, #E5E7EB)", borderRadius: 8, padding: "8px 10px",
                        marginBottom: 6,
                        background: e?.action === "removed" ? "var(--p-crit-soft, #FDECEC)"
                          : e ? "var(--p-warn-soft, #FFF7E6)"
                          : autoRefs.has(c.ref) ? "var(--p-info-soft, #EEF4FF)" : undefined }}>
                        <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
                          <b style={e?.action === "removed" ? { textDecoration: "line-through" } : undefined}>
                            {c.title || "Untitled clause"}
                          </b>
                          {c.checks > 0 && <span className="sub">{c.checks} check{c.checks === 1 ? "" : "s"}</span>}
                          {c.page && <span className="sub">p. {c.page}</span>}
                          {autoRefs.has(c.ref) && !e && (
                            <span className="sub">— re-worded with its term (see below)</span>
                          )}
                          <span style={{ marginLeft: "auto", display: "inline-flex", gap: 10 }}>
                            {!e && (<>
                              <span className="linkish" role="button" onClick={() => editClause(c)}>Edit</span>
                              <span className="linkish" role="button"
                                    onClick={() => setEdits(x => ({ ...x, [c.ref]: { action: "removed",
                                      title: c.title ?? "", text: c.body ?? c.text } }))}>
                                Delete
                              </span>
                            </>)}
                            {e && (
                              <span className="linkish" role="button" onClick={() => undoClause(c.ref)}
                                    style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
                                <Undo2 size={12} /> Undo
                              </span>
                            )}
                          </span>
                        </div>
                        {e?.action === "amended" ? (
                          <div style={{ marginTop: 6 }}>
                            <div className="field" style={{ marginBottom: 6 }}>
                              <label>Heading</label>
                              <input value={e.title}
                                     onChange={ev => setEdits(x => ({ ...x, [c.ref]: { ...e, title: ev.target.value } }))} />
                            </div>
                            <div className="field" style={{ marginBottom: 0 }}>
                              <label>New wording <span className="muted" style={{ fontWeight: 500 }}>— as it will read from the change date</span></label>
                              {authored ? (
                                <WordingEditor sectionKey={`mt:${c.ref}`} body={e.text}
                                               tokens={start.tokens ?? {}} labels={tokenLabels}
                                               onChange={text => setEdits(x => ({ ...x, [c.ref]: { ...e, text } }))} />
                              ) : (
                                <textarea rows={Math.min(14, Math.max(3, e.text.split("\n").length + 1))}
                                          value={e.text}
                                          onChange={ev => setEdits(x => ({ ...x, [c.ref]: { ...e, text: ev.target.value } }))} />
                              )}
                            </div>
                            <div className="sub" style={{ marginTop: 4 }}>
                              Today it says: <span style={{ whiteSpace: "pre-wrap" }}>{c.text}</span>
                            </div>
                          </div>
                        ) : (
                          <div style={{ whiteSpace: "pre-wrap", color: "var(--p-ink)", marginTop: 4,
                                        ...(e?.action === "removed" ? { textDecoration: "line-through", opacity: .7 } : {}) }}>
                            {long && !expanded ? lines.slice(0, SHOWN).join("\n").slice(0, 420) + "…" : c.text}
                            {long && (
                              <span className="linkish" role="button" style={{ marginLeft: 6 }}
                                    onClick={() => setOpen(o => ({ ...o, [c.ref]: !expanded }))}>
                                {expanded ? "less" : "more"}
                              </span>
                            )}
                          </div>
                        )}
                        {e?.action === "removed" && (
                          <div className="sub" style={{ marginTop: 4 }}>Deleted from the change date{c.checks
                            ? ` — its ${c.checks} check${c.checks === 1 ? "" : "s"} end the day before` : ""}.</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>

            {added.map((a, i) => (
              <div key={a.key} style={{ border: "1px dashed var(--p-line, #CBD5E1)", borderRadius: 8,
                                         padding: "8px 10px", marginTop: 8,
                                         background: "var(--p-ok-soft, #EEFBF3)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <b>New clause</b>
                  <span className="linkish" role="button" style={{ marginLeft: "auto", display: "inline-flex",
                                                                    alignItems: "center", gap: 3 }}
                        onClick={() => setAdded(x => x.filter((_, j) => j !== i))}>
                    <Trash2 size={12} /> Remove
                  </span>
                </div>
                <div className="grid g-2" style={{ marginTop: 6 }}>
                  <div className="field" style={{ marginBottom: 6 }}>
                    <label>Heading</label>
                    <input value={a.title} placeholder="Name this clause"
                           onChange={ev => setAdded(x => x.map((y, j) => j === i ? { ...y, title: ev.target.value } : y))} />
                  </div>
                  {!authored && (
                    <div className="field" style={{ marginBottom: 6 }}>
                      <label>Under the section</label>
                      <input value={a.section} list="mt-sections" placeholder="e.g. Underwriting Guidelines"
                             onChange={ev => setAdded(x => x.map((y, j) => j === i ? { ...y, section: ev.target.value } : y))} />
                    </div>
                  )}
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Wording</label>
                  {authored ? (
                    <WordingEditor sectionKey={`mt:new:${a.key}`} body={a.text}
                                   tokens={start.tokens ?? {}} labels={tokenLabels}
                                   onChange={text => setAdded(x => x.map((y, j) => j === i ? { ...y, text } : y))} />
                  ) : (
                    <textarea rows={4} value={a.text} placeholder="Write the clause as it will read in the contract."
                              onChange={ev => setAdded(x => x.map((y, j) => j === i ? { ...y, text: ev.target.value } : y))} />
                  )}
                </div>
              </div>
            ))}
            <datalist id="mt-sections">{sectionNames.map(n => <option key={n} value={n} />)}</datalist>
            <button className="btn sm" type="button" style={{ marginTop: 10 }}
                    onClick={() => setAdded(x => [...x, { key: `n${Date.now()}`, title: "", section: "", text: "" }])}>
              <Plus size={13} /> {authored ? "Add a section" : "Add a clause"}
            </button>
          </div>
        </div>

        )}

        {/* 4. The checks */}
        {curTab === "checks" && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-h">
            <h3>Checks</h3>
            <InfoTip text="Nothing is overwritten. A check that changes keeps running for rows up to
              the day before the change, and its new version runs from the change date. Every
              other check stays exactly as it is." />
          </div>
          <div style={{ padding: "4px 18px 16px", fontSize: 13 }}>
            {!pv || (!pv.changes.length && !pv.clause_changes.length) ? (
              <div className="hint">Change a term or a clause to see which checks move.</div>
            ) : (<>
              <ul style={{ margin: "4px 0 8px", paddingLeft: 18, lineHeight: 1.8 }}>
                {(r?.opening ?? []).map((o, i) => {
                  const old = o.replaces ? closingById.get(o.replaces) : undefined;
                  return (
                    <li key={`o${i}`}>
                      <b>{o.rule_name}</b>
                      {old ? <> — <b>{old.value}</b> until {old.until} → <b>{o.value}</b> from {o.from}</>
                        : <> — new check: <b>{o.value}</b> from {o.from}</>}
                      {o.switched_off && <span className="sub"> (switched off, as the check it replaces)</span>}
                    </li>
                  );
                })}
                {(r?.closing ?? []).filter(c => !replaced.has(c.rule_id)).map(c => (
                  <li key={`c${c.rule_id}`}>
                    <b>{c.rule_name}</b> — ends after {c.until}
                    {c.clause && <span className="sub"> (its clause “{c.clause}” is re-worded or deleted)</span>}
                  </li>
                ))}
                {(cc?.opening ?? []).map((o, i) => (
                  <li key={`n${i}`}>
                    <b>{o.rule_name ?? "New check"}</b>
                    {o.column && <span className="sub"> ({o.column})</span>} — new, from {o.from}, read from
                    “{o.clause}”{o.needs_review && <span className="sub"> — saved for review: </span>}
                    {o.message && <div className="sub" style={{ marginLeft: 2 }}>{o.message}</div>}
                  </li>
                ))}
                {(r?.unchecked ?? []).map((u, i) => (
                  <li key={`u${i}`} style={{ color: "var(--p-muted)" }}>
                    {u.question} {u.to} — not checked: {u.reason}
                  </li>
                ))}
              </ul>
              {cc?.state === "not_worked_out" && (
                <div className="note" style={{ marginTop: 6 }}>
                  The checks the changed clauses make are worked out by the same reading Bordereau
                  Setup uses — on Send, or now to see them first.{" "}
                  <button className="btn sm" type="button" disabled={!!busy} onClick={workOut}>
                    {busy === "checks" ? "Working out the checks… (up to a couple of minutes)" : "Work out the checks now"}
                  </button>
                </div>
              )}
              {cc?.state === "no_setup" && (
                <div className="hint">This contract has no Bordereau Setup yet, so no check is written now —
                  the new wording is what its setup will read.</div>
              )}
              {cc?.state === "failed" && (
                <div className="note warn" style={{ marginTop: 6 }}>{cc.error}{" "}
                  <button className="btn sm" type="button" disabled={!!busy} onClick={workOut}>Try again</button>
                </div>
              )}
              {!!cc?.no_column?.length && (
                <div className="note warn" style={{ marginTop: 6 }}>
                  <b>No check can be made yet for:</b>
                  <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                    {cc.no_column.map((n, i) => <li key={i}>“{n.clause}” — {n.reason}</li>)}
                  </ul>
                  <div className="sub" style={{ marginTop: 4 }}>The setup's output template has no column for it.
                    Once the change is live you can set up Bordereau Setup with a template that has one.</div>
                </div>
              )}
              {(r?.needs_choice ?? []).map(n => (
                <div className="note warn" key={n.key} style={{ marginTop: 8 }}>
                  <b>Which check is “{n.question}”?</b> Kavachio found more than one.
                  {n.candidates.map(cand => (
                    <label key={cand.rule_id} style={{ display: "block", marginTop: 4 }}>
                      <input type="radio" name={`choice-${n.key}`}
                             checked={(choices[n.key] ?? []).includes(cand.rule_id)}
                             onChange={() => setChoices(c => ({ ...c, [n.key]: [cand.rule_id] }))} />
                      {" "}{cand.rule_name} <span className="sub">({cand.column})</span>
                    </label>
                  ))}
                  <label style={{ display: "block", marginTop: 4 }}>
                    <input type="radio" name={`choice-${n.key}`}
                           checked={choices[n.key]?.length === 0}
                           onChange={() => setChoices(c => ({ ...c, [n.key]: [] }))} />
                    {" "}None of these — add a new check
                  </label>
                </div>
              ))}
              <div className="hint">
                {pv.checks_unchanged} other check{pv.checks_unchanged === 1 ? "" : "s"} stay exactly as they are.
              </div>
            </>)}
          </div>
        </div>
        )}

        {/* 5. Bordereau Setup: the date each row is checked by */}
        {curTab === "setup" && setupsShown && (
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-h">
              <h3>Bordereau Setup</h3>
              <InfoTip text="Each row is checked by the version in force on the date in this
                column — its transaction effective date. Saved on the setup when you send." />
            </div>
            <div style={{ padding: "4px 18px 16px" }}>
              {(pv?.setups ?? start.setups ?? []).map(s => (
                <div className="field" key={s.pipeline_id} style={{ maxWidth: 520 }}>
                  <label>{s.name} — transaction effective date column</label>
                  <select value={dateCols[String(s.pipeline_id)] ?? ""}
                          onChange={e => setDateCols(d => ({ ...d, [String(s.pipeline_id)]: e.target.value }))}>
                    <option value="">Choose the column…</option>
                    {(s.columns ?? []).map(c => (
                      <option key={c} value={c}>
                        {c}{c === s.suggested ? " — mapped to Transaction Effective Date" : ""}
                      </option>
                    ))}
                  </select>
                  {!s.suggested && (
                    <div className="hint">No column of this setup's template is mapped to the
                      transaction effective date — choose the one that holds it.</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* 6. Files already checked */}
        {curTab === "files" && !!pv?.recheck?.length && (
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-h">
              <h3>Files already checked</h3>
              <InfoTip text="These may hold rows dated on or after the change. Once it is live they
                are listed on the contract for a re-check — never re-run by themselves." />
            </div>
            <ul style={{ margin: "4px 18px 16px", paddingLeft: 18, fontSize: 13 }}>
              {pv.recheck.map(f => (
                <li key={f.export_id}>{f.filename}{f.period ? ` · ${f.period}` : ""}</li>
              ))}
            </ul>
          </div>
        )}

        {/* 7. The change document */}
        {curTab !== "preview" ? null : pv?.document ? (
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-h"><h3>The change document</h3></div>
            <div style={{ padding: "4px 18px 16px", fontSize: 13 }}>
              Your uploaded document <b>{pv.document.filename}</b> is the change document.
              {pv.signed ? " It is signed by both sides, so it is accepted rather than sent."
                : " The broker agrees it and both sides sign it."}
            </div>
          </div>
        ) : !!pv?.sections?.length && (
          <div className="card" style={{ marginBottom: 16 }}>
            <div className="card-h"><h3>The change document</h3></div>
            <div style={{ padding: "4px 18px 16px", fontSize: 13 }}>
              {pv.sections.map(sec => (
                <div key={sec.key} style={{ marginBottom: 10 }}>
                  <b>{sec.title}</b>
                  <div style={{ whiteSpace: "pre-wrap", color: "var(--p-ink)" }}>{sec.body}</div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Always in view, whichever tab is open: what still stops the change —
            each item opens the tab it is settled on — and the button. */}
        <div style={{ position: "sticky", bottom: 0, zIndex: 2, background: "var(--p-canvas)",
                      borderTop: "1px solid var(--p-border)", padding: "12px 0 14px" }}>
        {msg && <div className="note warn" style={{ marginBottom: 12 }}>{msg}</div>}
        {!!pv?.blockers?.length && (
          <div className="note" style={{ marginBottom: 12 }}>
            <b>Before you can {pv.signed ? "accept" : "send"} it:</b>
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {pv.blockers.map((b, i) => {
                const to = BLOCKER_TAB[b.code];
                const label = to ? tabs.find(t => t.key === to)?.label : undefined;
                return (
                  <li key={i}>
                    {b.message}
                    {to && label && curTab !== to && (
                      <> <span className="linkish" role="button" onClick={() => setTab(to)}>
                        Open “{label}” →</span></>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <button className="btn pri" type="button" disabled={!pv?.ok || !!busy} onClick={send}>
            <Send size={13} /> {busy === "send" ? (pv?.signed ? "Accepting…" : "Sending… (working out the checks)")
              : pv?.signed ? "Accept the signed change"
              : revising ? "Send the revised change to the broker" : "Send to the broker"}
          </button>
          <span className="hint" style={{ margin: 0 }}>
            {pv?.signed ? "It goes live once the carrier admin accepts it; the broker is emailed then."
              : "The broker agrees it, then both sides sign. Until then, nothing changes."}
          </span>
        </div>
        </div>
      </>)}
    </div></div>
  );
}

/** The `read` half of an uploaded document's answer, when Kavachio could read it. */
type MidTermStartRead = {
  effective_from: string | null; reason: string | null;
  terms: Record<string, string | number>; clauses: MidTermClauseEdit[];
};
