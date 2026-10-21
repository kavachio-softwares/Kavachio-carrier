/**
 * Exception resolution for a reinsurance bordereau — the broker's Exception
 * Triage and Rule Review screens, for the rows the treaty's rules caught.
 *
 *   /reinsurance/bordereaux/:id/exceptions           Exception Triage: one card per rule
 *   /reinsurance/bordereaux/:id/exceptions/:ruleId   Rule Review: its rows, decided one by one
 *
 * Each row is decided as the broker's are: APPROVE (the value the rule
 * recommends — the treaty's limit, its currency…), FIX (a value entered) or
 * DISMISS (kept as it is). Save decisions records them; FIX & VALIDATE
 * prepares the statement again with the values written into the rows and
 * checks every row again — a value that still breaks its rule is open again.
 * A Critical rule stops Send while a row of it is open, and Send waits for
 * Fix & Validate once decisions are recorded.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Check, CheckCircle2, ChevronDown, ChevronRight, Hand, RotateCcw, Wrench } from "lucide-react";
import Button from "../components/ui/Button";
import DecidedBy from "../components/DecidedBy";
import { fieldErrors } from "../api/contractRecord";
import {
  decideCeded, getCeded, validateCeded, type CededDecision, type CededDetail,
  type CheckItem, type TreatyCheck,
} from "../api/ceded";
import { Modal } from "../components/ui/Modal";
import { periodLabel } from "../utils/date";

const SEV_SPINE: Record<string, string> = { critical: "crit", blocking: "crit", warning: "warn" };
const SEV_BADGE: Record<string, string> = { critical: "b-crit", blocking: "b-crit", warning: "b-warn" };
const SEV_LABEL: Record<string, string> = { critical: "Critical", blocking: "Critical", warning: "Warning" };

// "exclude" (left out of the statement) is shown where it was saved; it is
// not offered — the broker's three decisions are.
type Kind = "none" | "approve" | "fix" | "dismiss" | "exclude";
type Pick = "approve" | "fix" | "dismiss";
const KIND_OF: Record<string, Kind> = {
  open: "none", approved: "approve", fixed: "fix", dismissed: "dismiss", excluded: "exclude" };
const PILL: Record<Kind, string> = {
  none: "pill pill-grey", approve: "pill pill-green", fix: "pill pill-blue",
  dismiss: "pill pill-amber", exclude: "pill pill-grey" };
const LABEL: Record<Kind, string> = {
  none: "Select decision", approve: "Approved", fix: "Fixed", dismiss: "Dismissed", exclude: "Left out" };
const OPTIONS: { kind: Pick; label: string; desc: string; icon: React.ReactNode; tone: string }[] = [
  { kind: "approve", label: "Approve", desc: "Accept the recommended value", icon: <Check size={14} />, tone: "text-emerald-600" },
  { kind: "fix", label: "Fix…", desc: "Enter a corrected value", icon: <Wrench size={14} />, tone: "text-blue-600" },
  { kind: "dismiss", label: "Dismiss (keep anyway)", desc: "Keep the actual value", icon: <Hand size={14} />, tone: "text-amber-600" },
];

const decidable = (c: TreatyCheck) => c.severity !== "untested" && !!c.items?.length;
const isCrit = (c: { severity: string }) => c.severity === "critical" || c.severity === "blocking";
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export default function CededExceptions() {
  const { id = "", ruleId } = useParams();
  const cbId = Number(id);
  const [d, setD] = useState<CededDetail | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    setD(null);
    getCeded(cbId).then(setD).catch(e => setErr(fieldErrors(e).message));
  }, [cbId]);

  if (!d) {
    return (
      <div className="proto"><div className="view full">
        {err ? <div className="note warn">{err}</div> : <div className="sub">Loading…</div>}
      </div></div>
    );
  }
  const check = ruleId ? (d.checks ?? []).find(c => c.id === ruleId && decidable(c)) : undefined;
  return ruleId ? <RuleReview d={d} check={check} onSaved={setD} /> : <Triage d={d} onSaved={setD} />;
}

/** Who and what the statement is about — the broker's about strip. */
function AboutStrip({ d }: { d: CededDetail }) {
  const programmes = [...new Set((d.sources ?? []).map(s => s.programme).filter(Boolean))];
  const reinsurers = d.statements.map(s => s.reinsurer).filter(Boolean);
  return (
    <div className="about-strip">
      <span className="chip lead">
        {periodLabel(d.period)}{d.version_no > 1 && ` · Version ${d.version_no}`}
      </span>
      <span className="chip"><span className="k">Treaty</span>{d.treaty}</span>
      {programmes.length > 0 && <span className="chip"><span className="k">Programme</span>{programmes.join(", ")}</span>}
      {reinsurers.length > 0 && <span className="chip"><span className="k">Reinsurer</span>{reinsurers.join(", ")}</span>}
    </div>
  );
}

function exportCsv(d: CededDetail, checks: TreatyCheck[]) {
  const q = (v: unknown) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [["Rule", "Severity", "Policy", "Actual", "Recommendation", "Reason", "Decision", "Output", "Decided by"]];
  for (const c of checks) {
    for (const it of c.items ?? []) {
      const k = KIND_OF[it.status ?? "open"];
      rows.push([c.label, SEV_LABEL[c.severity] ?? c.severity, it.policy ?? "", it.value ?? "",
        c.recommendation ?? (c.options ?? []).join(" | "), it.why,
        k === "none" ? "Pending" : LABEL[k], it.new_value ?? (k === "dismiss" ? it.value ?? "" : ""),
        it.decided_by ?? ""]);
    }
  }
  const blob = new Blob([rows.map(r => r.map(q).join(",")).join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `ceded_${d.id}_exceptions.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── screen A: Exception Triage ────────────────────────────────────────────
function Triage({ d, onSaved }: { d: CededDetail; onSaved: (d: CededDetail) => void }) {
  const nav = useNavigate();
  const fv = useFixAndValidate(d, onSaved);
  const [tab, setTab] = useState<"exceptions" | "summary">("exceptions");
  const [search, setSearch] = useState("");
  const checks = (d.checks ?? []).filter(decidable);
  const items = checks.flatMap(c => c.items ?? []);
  const crit = checks.filter(isCrit).reduce((n, c) => n + (c.items?.length ?? 0), 0);
  const open = checks.reduce((n, c) => n + (c.open ?? 0), 0);
  const q = search.trim().toLowerCase();
  const shown = checks.filter(c => !q
    || [c.label, c.clause ?? "", c.field ?? "", ...(c.items ?? []).map(it => it.policy ?? "")]
      .some(x => x.toLowerCase().includes(q)));
  const viewOnly = d.status !== "draft";

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Exception Triage <span className="tag-pill">CEDED-{d.id}</span></h2>
            <p>
              {plural(items.length, "exception")} across {plural(checks.length, "rule")} of the treaty.
            </p>
            <AboutStrip d={d} />
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav(`/reinsurance/bordereaux/${d.id}`)}>← Back to statement</button>
            {items.length > 0 && <button className="btn" onClick={() => exportCsv(d, checks)}>Export CSV</button>}
            {d.can_send && (
              <button className="btn" onClick={() => nav(`/reinsurance/bordereaux/${d.id}`)}>Send to reinsurers →</button>
            )}
            <button className="btn pri" onClick={fv.run} disabled={viewOnly || fv.busy}
              title={viewOnly ? "View only — this statement has been sent." : undefined}>
              {fv.busy ? "Validating…" : "Fix & Validate"}
            </button>
          </div>
        </div>

        {viewOnly ? (
          <div className="note" style={{ marginBottom: 18 }}>
            <b>View only.</b> This statement has been {d.status === "superseded" ? "replaced" : "sent"} —
            what was decided is shown as it was.
          </div>
        ) : d.unapplied ? (
          <div className="note warn" style={{ marginBottom: 18 }}>
            <b>{plural(d.unapplied, "decision")} recorded — not in the statement yet.</b>{" "}
            Run <b>Fix &amp; Validate</b> to write the approved and fixed values into the
            statement and check every row again.
          </div>
        ) : !!d.send_blocked_by?.length && (
          <div className="note warn" style={{ marginBottom: 18 }}>
            <b>Not ready to send.</b> A Critical rule has rows still open: {d.send_blocked_by.join("; ")}.
            Approve, Fix or Dismiss each row, then Fix &amp; Validate.
          </div>
        )}
        {fv.err && <div className="note warn" style={{ marginBottom: 18 }}>{fv.err}</div>}
        {fv.done}

        <div className="tabs" role="tablist">
          <button role="tab" className={tab === "exceptions" ? "on" : ""} onClick={() => setTab("exceptions")}>Exceptions</button>
          <button role="tab" className={tab === "summary" ? "on" : ""} onClick={() => setTab("summary")}>Check Summary</button>
        </div>

        {tab === "summary" ? <CheckSummary d={d} /> : checks.length === 0 ? (
          <div className="card">
            <div className="empty" style={{ color: "var(--p-ok-ink)" }}>
              ✓ No exceptions — every row keeps to the treaty&rsquo;s rules.
            </div>
          </div>
        ) : (
          <>
            <div className="tiles five" style={{ marginBottom: 18 }}>
              <div className="tile"><div className="k">Total</div><div className="v">{items.length}</div></div>
              <div className="tile"><div className="k">Critical</div><div className="v" style={{ color: "var(--p-crit)" }}>{crit}</div></div>
              <div className="tile"><div className="k">Warning</div><div className="v" style={{ color: "var(--p-warn)" }}>{items.length - crit}</div></div>
              <div className="tile"><div className="k">Resolved</div><div className="v" style={{ color: "var(--p-ok)" }}>{items.length - open}</div></div>
              <div className="tile"><div className="k">Open</div><div className="v">{open}</div></div>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
              <div className="search" style={{ minWidth: 280 }}>
                <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="m20 20-3-3" /></svg>
                <input value={search} onChange={e => setSearch(e.target.value)} placeholder="Search rule, field, policy, clause…" />
              </div>
              {search && <span className="linkish" onClick={() => setSearch("")}>Clear</span>}
              <span className="muted" style={{ marginLeft: "auto", fontSize: 12 }}>
                {plural(shown.length, "rule")} shown
              </span>
            </div>

            <div className="grid">
              {shown.map(c => (
                <div key={c.id} className={`card spine ${SEV_SPINE[c.severity] ?? "info"} click`}
                     onClick={() => nav(`/reinsurance/bordereaux/${d.id}/exceptions/${encodeURIComponent(c.id)}`)}>
                  <div className="card-h">
                    <h3>{c.label}</h3>
                    <div className="right">
                      <span className={`badge ${SEV_BADGE[c.severity] ?? "b-info"}`}>
                        <span className="d" />{SEV_LABEL[c.severity] ?? c.severity} · {plural(c.rows, "policy", "policies")}
                      </span>
                      <span className="linkish">{viewOnly ? "View →" : "Review →"}</span>
                    </div>
                  </div>
                  <div style={{ padding: "14px 20px" }}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
                      {c.field && <span className="badge b-mut"><span className="d" />Field: {c.field}</span>}
                      <span className="badge b-mut"><span className="d" />
                        {c.source === "wording" ? "Treaty wording" : "Treaty terms"}
                      </span>
                    </div>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <span className="badge b-ok">Approved {c.approved ?? 0}</span>
                      <span className="badge b-info">Fixed {c.fixed ?? 0}</span>
                      <span className="badge b-warn">Dismissed {c.dismissed ?? 0}</span>
                      {!!c.excluded && <span className="badge b-mut">Left out {c.excluded}</span>}
                      <span className="badge b-mut">Pending {c.open ?? 0}</span>
                    </div>
                  </div>
                </div>
              ))}
              {shown.length === 0 && (
                <div className="card"><div className="empty">No rules match &ldquo;{search}&rdquo;.</div></div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** Every rule of the treaty and how the rows fared against it. */
function CheckSummary({ d }: { d: CededDetail }) {
  const byId = new Map((d.checks ?? []).map(c => [c.id, c]));
  const rules = d.rules?.length ? d.rules
    : (d.checks ?? []).map(c => ({ id: c.id, label: c.label, severity: c.severity, source: c.source, clause: c.clause }));
  return (
    <div className="card">
      <div className="tbl-wrap">
        <table className="panel-tbl">
          <thead>
            <tr><th>Rule</th><th style={{ width: 110 }}>Severity</th><th style={{ width: 130 }}>From</th>
              <th style={{ width: 220 }}>Result</th></tr>
          </thead>
          <tbody>
            {rules.map(r => {
              const c = byId.get(r.id);
              const result = !c
                ? <span className="badge b-ok"><span className="d" />Passed</span>
                : c.severity === "untested"
                  ? <span className="badge b-mut" title={c.why}><span className="d" />Not tested</span>
                  : <span className={`badge ${c.open ? SEV_BADGE[c.severity] : "b-ok"}`}>
                      <span className="d" />{plural(c.rows, "exception")}{c.open ? ` · ${c.open} pending` : " · resolved"}
                    </span>;
              return (
                <tr key={r.id}>
                  <td>
                    <b style={{ fontWeight: 600 }}>{r.label}</b>
                    {r.clause && <div className="sub"><i>&ldquo;{r.clause}&rdquo;</i></div>}
                    {c?.severity === "untested" && c.why && <div className="sub">Not tested: {c.why}.</div>}
                  </td>
                  <td>{r.severity === "untested" ? "—" : (
                    <span className={`badge ${SEV_BADGE[r.severity] ?? "b-mut"}`}><span className="d" />{SEV_LABEL[r.severity] ?? r.severity}</span>)}</td>
                  <td className="sub">{r.source === "wording" ? "Treaty wording" : "Treaty terms"}</td>
                  <td>{result}</td>
                </tr>
              );
            })}
            {rules.length === 0 && <tr><td colSpan={4}><div className="empty">This treaty has no rules.</div></td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── screen B: Rule Review ─────────────────────────────────────────────────
function RuleReview({ d, check, onSaved }: {
  d: CededDetail; check?: TreatyCheck; onSaved: (d: CededDetail) => void;
}) {
  const nav = useNavigate();
  const back = () => nav(`/reinsurance/bordereaux/${d.id}/exceptions`);
  const [showDetails, setShowDetails] = useState(false);

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>{check ? check.label : "Rule Review"}</h2>
            {check && (
              <p className="mono">
                {check.id.toUpperCase()} · {SEV_LABEL[check.severity] ?? check.severity} · {check.rows} affected {check.rows === 1 ? "policy" : "policies"}
              </p>
            )}
          </div>
          <div className="actions">
            <button className="btn" onClick={back}>← All Exceptions</button>
          </div>
        </div>

        {!check ? (
          <div className="card pad">
            <p className="muted" style={{ fontSize: 13, margin: 0 }}>
              No exceptions for this rule. <span className="linkish" onClick={back}>Back To Exceptions</span>
            </p>
          </div>
        ) : (
          <>
            <div className={`card spine ${SEV_SPINE[check.severity] ?? "info"} pad`} style={{ marginBottom: 18 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 10 }}>
                <span className={`badge ${SEV_BADGE[check.severity] ?? "b-info"}`}>
                  <span className="d" />{SEV_LABEL[check.severity] ?? check.severity}
                </span>
                {check.field && <span className="tag-pill">{check.field}</span>}
                <span className="badge b-mut"><span className="d" />{check.source === "wording" ? "Treaty wording" : "Treaty terms"}</span>
                <span className="muted" style={{ marginLeft: "auto", fontSize: 12.5 }}>
                  {check.rows} {check.rows === 1 ? "policy" : "policies"} affected
                </span>
              </div>
              <div style={{ fontSize: 15, fontWeight: 600, color: "var(--p-ink)", lineHeight: 1.45 }}>
                {problemOf(check)}
              </div>
              {d.status === "draft" && (
                <div style={{ fontSize: 13, color: "var(--p-muted)", marginTop: 4 }}>
                  {check.recommendation
                    ? `Approve all to replace it with "${check.recommendation}", or Dismiss if the bordereau is right.`
                    : check.options?.length
                      ? "Choose the right value for each row with Approve or Fix, or Dismiss if the bordereau is right."
                      : "Use Fix to enter the right value, or Dismiss if the bordereau is right."}
                </div>
              )}
              {(check.clause || check.kind) && (
                <>
                  <button type="button" className="linkish" onClick={() => setShowDetails(o => !o)}
                    style={{ display: "inline-flex", alignItems: "center", gap: 4, marginTop: 10,
                             background: "none", border: 0, padding: 0, fontSize: 12.5, cursor: "pointer" }}>
                    {showDetails ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    {showDetails ? "Hide rule details" : "Show rule details"}
                  </button>
                  {showDetails && (
                    <div style={{ marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--p-border)", fontSize: 13 }}>
                      <div className="sub" style={{ marginBottom: 4 }}>
                        Where this comes from — {check.source === "wording" ? "the treaty wording" : "the treaty's terms"}
                      </div>
                      {check.clause
                        ? <div style={{ fontStyle: "italic", color: "var(--p-ink)" }}>&ldquo;{check.clause}&rdquo;</div>
                        : <div>{check.label}</div>}
                    </div>
                  )}
                </>
              )}
            </div>

            <div className="card-h" style={{ borderBottom: "none", paddingLeft: 0, paddingBottom: 4 }}>
              <h3>Decisions</h3>
              <span className="sub">{check.rows} {check.rows === 1 ? "policy" : "policies"}</span>
            </div>
            <DecisionTable key={check.id} d={d} check={check} onSaved={onSaved} />
          </>
        )}
      </div>
    </div>
  );
}

/** The rule's problem in one line — the broker's "Layer 1". */
function problemOf(c: TreatyCheck): string {
  const items = c.items ?? [];
  if (items.length === 1) return `Policy ${items[0].policy ?? "—"}: ${items[0].why}.`;
  const whys = [...new Set(items.map(it => it.why))];
  if (whys.length === 1) return `${c.rows} policies: ${whys[0]}.`;
  return `${c.rows} policies break this rule — for example ${items[0].policy ?? "a row"}: ${items[0].why}.`;
}

type Decision = { kind: Kind; value: string; reason: string };

/** Fix & Validate — the broker's: the statement prepared again with the
 *  decisions written in, every row checked again; "Done" when it is. */
function useFixAndValidate(d: CededDetail, onSaved: (d: CededDetail) => void) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  async function run() {
    setBusy(true); setErr("");
    try {
      const nd = await validateCeded(d.id);
      onSaved(nd);
      const open = (nd.checks ?? []).reduce((n, c) => n + (c.open ?? 0), 0);
      const still = (nd.checks ?? []).flatMap(c => c.items ?? []).filter(it => it.still_fails).length;
      setMsg(`The statement was prepared again with your decisions and every row checked again. `
        + (still ? `${plural(still, "corrected value")} still break${still === 1 ? "s" : ""} the rule — open again. ` : "")
        + (open ? `${plural(open, "exception")} still open.` : nd.can_send ? "It is ready to send." : "No exception is open."));
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }
  const done = (
    <Modal open={!!msg} title="Done" size="sm" onClose={() => setMsg("")}
      footer={<div className="flex justify-center w-full">
        <button className="btn pri" style={{ minWidth: 96, display: "flex", justifyContent: "center" }}
                onClick={() => setMsg("")}>OK</button></div>}>
      <div className="flex flex-col items-center text-center gap-3 py-2">
        <div className="flex items-center justify-center w-14 h-14 rounded-full bg-emerald-100">
          <CheckCircle2 size={30} className="text-emerald-600" strokeWidth={2.25} />
        </div>
        <p className="text-[15px] font-medium text-ink leading-relaxed">{msg}</p>
      </div>
    </Modal>
  );
  return { run, busy, err, done };
}

/** What Approve writes for a row — the rule's one value, else nothing to approve. */
const approvable = (c: TreatyCheck) => !!c.recommendation || !!c.options?.length;

function DecisionTable({ d, check, onSaved }: {
  d: CededDetail; check: TreatyCheck; onSaved: (d: CededDetail) => void;
}) {
  const items = check.items ?? [];
  const readOnly = d.status !== "draft";
  const fv = useFixAndValidate(d, onSaved);
  const saved = useMemo<Record<string, Decision>>(() => Object.fromEntries(items.map(it =>
    [it.key, { kind: KIND_OF[it.status ?? "open"], value: it.new_value ?? "", reason: it.reason ?? "" }])), [items]);
  const [decisions, setDecisions] = useState<Record<string, Decision>>(saved);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkMenu, setBulkMenu] = useState(false);
  const [bulkPrompt, setBulkPrompt] = useState<"approve" | "fix" | null>(null);
  const [bulkValue, setBulkValue] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [recorded, setRecorded] = useState<{ approve: number; fix: number; dismiss: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  useEffect(() => { setDecisions(saved); setSelected(new Set()); }, [saved]);

  const changed = items.filter(it => {
    const a = decisions[it.key], b = saved[it.key];
    return a.kind !== b.kind || ((a.kind === "approve" || a.kind === "fix") && a.value.trim() !== b.value.trim());
  });
  const counts = { approve: 0, fix: 0, dismiss: 0 };
  for (const it of items) {
    const k = decisions[it.key].kind;
    if (k === "approve" || k === "fix" || k === "dismiss") counts[k]++;
  }
  const decided = items.filter(it => decisions[it.key].kind !== "none").length;
  const allChecked = items.length > 0 && items.every(it => selected.has(it.key));
  const someChecked = selected.size > 0 && !allChecked;
  const recoValue = check.recommendation ?? "";

  const setDecision = (key: string, nd: Decision) => setDecisions(m => ({ ...m, [key]: nd }));
  const toggleOne = (key: string) => setSelected(s => {
    const n = new Set(s); n.has(key) ? n.delete(key) : n.add(key); return n;
  });
  const setMany = (keys: Iterable<string>, nd: Decision) => setDecisions(m => {
    const n = { ...m };
    for (const k of keys) n[k] = { ...nd, reason: m[k]?.reason ?? "" };
    return n;
  });

  function openBulk(kind: Pick) {
    setBulkMenu(false);
    if (kind === "dismiss") { setMany(selected, { kind: "dismiss", value: "", reason: "" }); setSelected(new Set()); return; }
    if (kind === "approve" && !check.options?.length) {
      setMany(selected, { kind: "approve", value: recoValue, reason: "" }); setSelected(new Set()); return;
    }
    setBulkValue(kind === "fix" ? recoValue : "");
    setBulkPrompt(kind);
  }
  function applyBulk() {
    if (!bulkPrompt) return;
    setMany(selected, { kind: bulkPrompt, value: bulkValue.trim(), reason: "" });
    setBulkPrompt(null); setBulkValue(""); setSelected(new Set());
  }

  async function save() {
    setSaving(true); setSaveErr(null);
    try {
      const out: CededDecision[] = changed.map(it => {
        const v = decisions[it.key];
        return v.kind === "none" ? { key: it.key, kind: "reopen" }
          : { key: it.key, kind: v.kind as Pick, value: v.value.trim() || null, reason: v.reason.trim() || null };
      });
      const nd = await decideCeded(d.id, out);
      setRecorded({ ...counts });
      onSaved(nd);
    } catch (e) {
      setSaveErr(fieldErrors(e).message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="bg-white">
      {/* toolbar — light green, as the broker's selection / bulk-decision bar */}
      <div className={`sticky top-0 ${bulkMenu || bulkPrompt ? "z-30" : "z-20"} flex items-center gap-2 px-3 py-2 flex-wrap rounded-lg bg-emerald-50 border border-emerald-200`}>
        {readOnly ? (
          <span className="text-sm text-ink-muted">{decided}/{items.length} Decided · view only — this statement has been sent</span>
        ) : selected.size > 0 ? (
          <>
            <span className="text-sm font-medium">{selected.size} selected</span>
            <span className="text-sm text-ink-soft">Bulk Decision:</span>
            <div className="relative">
              <button onClick={() => setBulkMenu(o => !o)}
                className="inline-flex items-center gap-1.5 text-sm font-medium px-2 py-1 rounded-md border border-border bg-white hover:bg-surface-2 transition text-ink-muted">
                Choose decision <ChevronDown size={14} className="text-ink-soft" />
              </button>
              {bulkMenu && (
                <>
                  <div className="fixed inset-0 z-10" onClick={() => setBulkMenu(false)} />
                  <div className="absolute z-20 mt-1 w-64 rounded-lg border border-border bg-white shadow-lg p-1">
                    {OPTIONS.map(opt => {
                      const off = opt.kind === "approve" && !approvable(check);
                      return (
                        <button key={opt.kind} onClick={() => openBulk(opt.kind)} disabled={off}
                          className={`w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-md ${off ? "opacity-40 cursor-not-allowed" : "hover:bg-surface-2"}`}>
                          <span className={`mt-0.5 ${opt.tone}`}>{opt.icon}</span>
                          <span>
                            <span className="block text-xs font-medium">{opt.label}</span>
                            <span className="block text-[11px] text-ink-soft">{off ? "No recommended value — use Fix" : opt.desc}</span>
                          </span>
                        </button>
                      );
                    })}
                    <button onClick={() => { setBulkMenu(false); setMany(selected, { kind: "none", value: "", reason: "" }); setSelected(new Set()); }}
                      className="w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-md hover:bg-surface-2">
                      <span className="mt-0.5 text-ink-soft"><RotateCcw size={14} /></span>
                      <span>
                        <span className="block text-xs font-medium">Reset to pending</span>
                        <span className="block text-[11px] text-ink-soft">Undo the decision</span>
                      </span>
                    </button>
                  </div>
                </>
              )}
            </div>
            <button onClick={() => setSelected(new Set())} className="text-xs underline text-ink-muted ml-1">Clear</button>
          </>
        ) : (
          <>
            <span className="text-sm text-ink-muted">{decided}/{items.length} Decided</span>
            <Button variant="secondary" disabled={!check.recommendation}
              title={check.recommendation ? undefined : "No single recommended value for this rule — use Fix"}
              onClick={() => setMany(items.filter(it => decisions[it.key].kind === "none").map(it => it.key),
                                     { kind: "approve", value: recoValue, reason: "" })}>
              <Check size={14} /> Approve all (recommended)
            </Button>
          </>
        )}
      </div>

      {/* bulk prompt — the value (Fix) or the choice (Approve) for all selected */}
      {bulkPrompt && (
        <div className="rounded-lg border border-border bg-white shadow-sm p-3 mt-2 text-xs">
          {bulkPrompt === "fix" ? (
            <>
              <div className="font-semibold text-blue-700 mb-1">Fix {selected.size} selected — value to apply to all</div>
              <input autoFocus value={bulkValue} onChange={ev => setBulkValue(ev.target.value)}
                placeholder={recoValue || "Corrected value…"} className="input py-1 text-xs w-full max-w-xs" />
            </>
          ) : (
            <>
              <div className="font-semibold text-emerald-700 mb-1">Approve {selected.size} selected — choose a value</div>
              <p className="text-[11px] text-ink-soft mb-1.5">Applied to every selected row.</p>
              <div className="max-h-40 overflow-y-auto space-y-0.5">
                {(check.options ?? []).map(o => (
                  <label key={o} className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-surface-2 cursor-pointer">
                    <input type="radio" name="bulk-approve" checked={bulkValue === o} onChange={() => setBulkValue(o)} className="h-3 w-3" />
                    <span className="font-mono">{o}</span>
                  </label>
                ))}
              </div>
            </>
          )}
          <div className="flex items-center justify-end gap-2 mt-3">
            <button onClick={() => setBulkPrompt(null)} className="text-xs px-2 py-1 rounded-md hover:bg-surface-2 text-ink-muted">Cancel</button>
            <Button onClick={applyBulk} disabled={!bulkValue.trim()}>Apply to {selected.size}</Button>
          </div>
        </div>
      )}

      {/* table */}
      <div className="overflow-x-auto rounded-lg border border-border mt-4">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-surface-2 text-ink-muted text-[11px] uppercase tracking-wide">
              <th className="px-3 py-2 w-8">
                {!readOnly && <input type="checkbox" className="h-3.5 w-3.5 align-middle" checked={allChecked}
                  ref={el => { if (el) el.indeterminate = someChecked; }}
                  onChange={() => setSelected(allChecked ? new Set() : new Set(items.map(it => it.key)))} />}
              </th>
              <th className="font-medium px-3 py-2">Policy</th>
              <th className="font-medium px-3 py-2">Actual</th>
              <th className="font-medium px-3 py-2">Recommendation</th>
              <th className="font-medium px-3 py-2">Reason</th>
              <th className="font-medium px-3 py-2">Decision</th>
              <th className="font-medium px-3 py-2">Output</th>
            </tr>
          </thead>
          <tbody>
            {items.map(it => {
              const dec = decisions[it.key];
              const out = outputFor(it, dec);
              const changedTo = (dec.kind === "approve" || dec.kind === "fix") && dec.value ? dec.value : null;
              return (
                <tr key={it.key} className={`border-t border-border align-top ${selected.has(it.key) ? "bg-blue-50/50" : ""}`}>
                  <td className="px-3 py-2">
                    {!readOnly && <input type="checkbox" className="h-3.5 w-3.5 align-middle"
                      checked={selected.has(it.key)} onChange={() => toggleOne(it.key)} />}
                  </td>
                  <td className="px-3 py-2 whitespace-nowrap"><div className="font-mono font-medium">{it.policy ?? "—"}</div></td>
                  <td className="px-3 py-2">
                    {changedTo ? (
                      <span className="whitespace-nowrap">
                        <span className="font-mono text-emerald-700">{changedTo}</span>
                        {it.value && <span className="ml-1.5 font-mono text-[10px] text-ink-soft line-through">{it.value}</span>}
                      </span>
                    ) : it.value ? <span className="font-mono text-red-700">{it.value}</span>
                      : <span className="italic text-ink-soft">(empty)</span>}
                    {it.still_fails && (
                      <div className="mt-1 text-[10px] text-danger">Still breaks the rule after Fix &amp; Validate</div>
                    )}
                  </td>
                  <td className="px-3 py-2 max-w-[220px]">
                    {check.recommendation
                      ? <span className="font-mono text-emerald-700">{check.recommendation}</span>
                      : check.options?.length
                        ? <span className="font-mono text-emerald-700 break-words">{check.options.join(" · ")}</span>
                        : <span className="text-amber-600 text-[11px] italic">No single value — use Fix</span>}
                  </td>
                  <td className="px-3 py-2 text-ink-muted max-w-[260px]">{it.why}</td>
                  <td className="px-3 py-2">
                    <DecisionCell check={check} decision={dec} readOnly={readOnly}
                      onChange={nd => setDecision(it.key, nd)} />
                    {saved[it.key].kind === dec.kind && dec.kind !== "none" && (
                      <DecidedBy by={it.decided_by} at={it.decided_at} />
                    )}
                  </td>
                  <td className="px-3 py-2"><span className={`font-mono ${out.tone}`}>{out.text}</span></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {check.rows > items.length && (
        <div className="text-[11px] text-ink-soft mt-2">Showing the first {items.length} of {check.rows} rows.</div>
      )}

      {/* bottom bar — sticky so Save is always reachable without scrolling */}
      <div className="sticky bottom-0 z-20 flex items-center gap-3 px-3 py-3 mt-2 border-t border-border flex-wrap
                      bg-white/95 supports-[backdrop-filter]:bg-white/80 backdrop-blur rounded-b-lg
                      shadow-[0_-2px_8px_rgba(15,23,42,0.06)]">
        <div className="flex items-center gap-1.5 flex-wrap text-[11px]">
          <span className="pill pill-green">Approved {counts.approve}</span>
          <span className="pill pill-blue">Fixed {counts.fix}</span>
          <span className="pill pill-amber">Dismissed {counts.dismiss}</span>
          <span className="pill pill-grey">Pending {items.length - decided}</span>
          <span className="text-ink-soft ml-1">{decided}/{items.length} decided</span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {(saveErr || fv.err) && <span className="text-[11px] text-danger">{saveErr || fv.err}</span>}
          {!readOnly && !!d.unapplied && !changed.length && (
            <Button variant="secondary" onClick={fv.run} disabled={fv.busy}>
              {fv.busy ? "Validating…" : "Fix & Validate"}
            </Button>
          )}
          {!readOnly && (
            <Button onClick={() => { setSaveErr(null); setConfirmOpen(true); }} disabled={!changed.length || saving}>
              {saving ? "Saving…" : "Save decisions"}
            </Button>
          )}
        </div>
      </div>

      {confirmOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={() => setConfirmOpen(false)}>
          <div className="w-full max-w-sm rounded-xl border border-border bg-white shadow-xl p-5" onClick={e => e.stopPropagation()}>
            <div className="font-semibold text-base">Save decisions?</div>
            <p className="text-sm text-ink-muted mt-1">
              You&rsquo;re about to save {plural(changed.length, "decision")} for this rule.
            </p>
            <div className="flex items-center gap-1.5 flex-wrap text-[11px] mt-3">
              {counts.approve > 0 && <span className="pill pill-green">Approved {counts.approve}</span>}
              {counts.fix > 0 && <span className="pill pill-blue">Fixed {counts.fix}</span>}
              {counts.dismiss > 0 && <span className="pill pill-amber">Dismissed {counts.dismiss}</span>}
            </div>
            {(counts.fix > 0 || counts.approve > 0) && (
              <p className="text-[11px] text-ink-soft mt-3">
                Fixed &amp; Approved values reach the statement only after you click{" "}
                <span className="font-medium">Fix &amp; Validate</span>.
              </p>
            )}
            <div className="flex justify-end gap-2 mt-5">
              <Button variant="ghost" onClick={() => setConfirmOpen(false)} disabled={saving}>Cancel</Button>
              <Button onClick={() => { setConfirmOpen(false); void save(); }} disabled={saving}>Confirm &amp; save</Button>
            </div>
          </div>
        </div>
      )}

      {/* Decisions Recorded — saved, not yet in the statement: Fix & Validate. */}
      {recorded && (() => {
        const parts = [recorded.approve ? `${recorded.approve} approved` : null,
          recorded.fix ? `${recorded.fix} fixed` : null,
          recorded.dismiss ? `${recorded.dismiss} dismissed` : null].filter(Boolean).join(" · ");
        return (
          <div className="proto-modal-overlay" onClick={() => setRecorded(null)}>
            <div className="proto-modal" onClick={e => e.stopPropagation()}>
              <div className="m-h">
                <h3>Decisions Recorded — {check.label}</h3>
                <button className="x" onClick={() => setRecorded(null)} aria-label="Close">×</button>
              </div>
              <div className="m-b">
                {parts && <div style={{ marginBottom: 10 }}><b>{parts}</b></div>}
                {recorded.approve + recorded.fix > 0 ? (
                  <>Your approved and fixed values are saved, but they are <b>not in the statement yet</b>.
                    Run <b>Fix &amp; Validate</b> to prepare it again with them and re-check every row.</>
                ) : (
                  <>Dismissed rows keep their values and are ceded as they are. Run <b>Fix &amp; Validate</b>{" "}
                    to take the decisions into the statement.</>
                )}
              </div>
              <div className="m-f">
                <button className="btn" onClick={() => setRecorded(null)}>Later</button>
                <button className="btn pri" disabled={fv.busy}
                        onClick={() => { setRecorded(null); void fv.run(); }}>Fix &amp; Validate</button>
              </div>
            </div>
          </div>
        );
      })()}
      {fv.done}
    </div>
  );
}

/** What the row puts in the statement, as decided. */
function outputFor(it: CheckItem, d: Decision): { text: string; tone: string } {
  if (d.kind === "approve") return { text: d.value || "—", tone: "text-emerald-700" };
  if (d.kind === "fix") return { text: d.value || "—", tone: "text-blue-700" };
  if (d.kind === "dismiss") return { text: it.value ?? "(empty)", tone: "text-slate-600" };
  if (d.kind === "exclude") return { text: "Left out", tone: "text-ink-soft" };
  return { text: "—", tone: "text-ink-soft" };
}

/** The broker's decision pill + popover: Approve / Fix… / Dismiss (keep anyway). */
function DecisionCell({ check, decision, onChange, readOnly }: {
  check: TreatyCheck; decision: Decision; onChange: (d: Decision) => void; readOnly: boolean;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [prompt, setPrompt] = useState<"approve" | "fix" | null>(null);
  const [draft, setDraft] = useState("");
  const ref = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null);
  const canApprove = approvable(check);

  function place(h: number) {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - 296));
    const below = window.innerHeight - r.bottom - 12;
    setPos(below < h && r.top > below ? { left, bottom: window.innerHeight - r.top + 4 } : { left, top: r.bottom + 4 });
  }
  const close = () => { setMenuOpen(false); setPrompt(null); };

  function pick(kind: Pick) {
    setMenuOpen(false);
    // Approve and Dismiss apply straight from the dropdown; Approve asks only
    // when the rule offers several values, Fix always asks for the value.
    if (kind === "dismiss") { onChange({ kind: "dismiss", value: "", reason: decision.reason }); return; }
    if (kind === "approve" && !check.options?.length) {
      onChange({ kind: "approve", value: check.recommendation ?? "", reason: decision.reason }); return;
    }
    setDraft(decision.kind === kind ? decision.value : kind === "fix" ? check.recommendation ?? "" : "");
    place(200);
    setPrompt(kind);
  }

  if (readOnly) {
    return <span className={PILL[decision.kind]}>{decision.kind === "none" ? "Pending" : LABEL[decision.kind]}</span>;
  }

  return (
    <div className="relative">
      <span ref={ref} className="inline-block">
        <button onClick={() => { if (menuOpen) { setMenuOpen(false); return; } setPrompt(null); place(220); setMenuOpen(true); }}
          className="inline-flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-md border border-border bg-white hover:bg-surface-2 transition">
          <span className={PILL[decision.kind]}>{LABEL[decision.kind]}</span>
          <ChevronDown size={12} className="text-ink-soft" />
        </button>
      </span>
      {(menuOpen || prompt) && <div className="fixed inset-0 z-30" onClick={close} />}
      {menuOpen && pos && (
        <div className="fixed z-40 w-64 rounded-lg border border-border bg-white shadow-lg p-1"
             style={{ left: pos.left, top: pos.top, bottom: pos.bottom }}>
          {OPTIONS.map(opt => {
            const off = opt.kind === "approve" && !canApprove;
            return (
              <button key={opt.kind} onClick={() => pick(opt.kind)} disabled={off}
                title={off ? "No recommended value to approve — use Fix" : undefined}
                className={`w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-md ${
                  off ? "opacity-40 cursor-not-allowed" : `hover:bg-surface-2 ${decision.kind === opt.kind ? "bg-surface-2" : ""}`}`}>
                <span className={`mt-0.5 ${opt.tone}`}>{opt.icon}</span>
                <span>
                  <span className="block text-xs font-medium">{opt.label}</span>
                  <span className="block text-[11px] text-ink-soft">{off ? "No recommended value — use Fix" : opt.desc}</span>
                </span>
              </button>
            );
          })}
          {decision.kind !== "none" && (
            <button onClick={() => { onChange({ kind: "none", value: "", reason: "" }); close(); }}
              className="w-full text-left flex items-start gap-2 px-2 py-1.5 rounded-md hover:bg-surface-2">
              <span className="mt-0.5 text-ink-soft"><RotateCcw size={14} /></span>
              <span>
                <span className="block text-xs font-medium">Reset to pending</span>
                <span className="block text-[11px] text-ink-soft">Undo the decision</span>
              </span>
            </button>
          )}
        </div>
      )}
      {prompt && pos && (
        <div className="fixed z-40 w-72 rounded-lg border border-border bg-white shadow-lg p-3 text-xs"
             style={{ left: pos.left, top: pos.top, bottom: pos.bottom }}>
          {prompt === "fix" ? (
            <>
              <div className="font-semibold text-blue-700 mb-1">Fix — enter the corrected value</div>
              {check.recommendation && (
                <p className="text-[11px] text-ink-soft mb-1.5">The rule allows at most / only{" "}
                  <span className="font-mono text-ink">{check.recommendation}</span>.</p>
              )}
              <input autoFocus value={draft} onChange={e => setDraft(e.target.value)}
                placeholder={check.recommendation ?? "Corrected value…"} className="input py-1 text-xs w-full" />
            </>
          ) : (
            <>
              <div className="font-semibold text-emerald-700 mb-1">Approve — choose a value</div>
              <div className="max-h-40 overflow-y-auto space-y-0.5">
                {(check.options ?? []).map(o => (
                  <label key={o} className="flex items-center gap-2 px-1.5 py-1 rounded hover:bg-surface-2 cursor-pointer">
                    <input type="radio" checked={draft === o} onChange={() => setDraft(o)} className="h-3 w-3" />
                    <span className="font-mono">{o}</span>
                  </label>
                ))}
              </div>
            </>
          )}
          <div className="flex items-center justify-end gap-2 mt-3">
            <button onClick={close} className="text-xs px-2 py-1 rounded-md hover:bg-surface-2 text-ink-muted">Cancel</button>
            <Button disabled={!draft.trim()}
              onClick={() => { onChange({ kind: prompt, value: draft.trim(), reason: decision.reason }); close(); }}>
              {prompt === "fix" ? "Fix" : "Approve"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
