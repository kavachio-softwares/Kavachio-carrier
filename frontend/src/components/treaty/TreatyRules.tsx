/**
 * A treaty's rules — the Treaty Rules step of its reinsurance bordereau
 * setup, and the checks they produce on a statement.
 *
 *   From the treaty terms   worked out from its structure: the period, the
 *                           currency, the data its type needs, its limit per
 *                           risk or capacity, duplicate policies.
 *   From the wording        read from the treaty's wording: excluded classes
 *                           and territories, the territory it covers, the most
 *                           on any one risk — each with its clause.
 *
 * On or off, and Critical (stops Send until dealt with) or Warning (shown,
 * sent anyway). Like a broker contract's rules, run on every bordereau.
 */
import { useState } from "react";
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Loader2, ScrollText, Search, Sparkles,
} from "lucide-react";
import { fieldErrors } from "../../api/contractRecord";
import {
  extractTreatyRules, type SetupRecord, type SetupValues, type TreatyCheck, type TreatyRule,
} from "../../api/ceded";

export function TreatyRulesStep({ cid, rec, v, change, readOnly = false }: {
  cid: number; rec: SetupRecord; v: SetupValues;
  change: (patch: Partial<SetupValues>) => void;
  /** The setup's read-only view: no switches, no reading the wording. */
  readOnly?: boolean;
}) {
  const rules = v.rules ?? [];
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  // The broker setup's Contract Rules list, for a treaty: filter chips, a
  // search, and one row per rule that opens to its clause and settings.
  const [filter, setFilter] = useState<"terms" | "wording" | "all">("all");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const set = (id: string, patch: Partial<TreatyRule>) =>
    change({ rules: rules.map(r => r.id === id ? { ...r, ...patch } : r) });

  async function read() {
    setBusy(true); setErr(""); setNote("");
    try {
      const r = await extractTreatyRules(cid);
      change({ rules: r.rules });
      setNote(r.found
        ? `${r.found} rule${r.found === 1 ? "" : "s"} read from ${r.read_from}. Check them, then save.`
        : `Nothing in ${r.read_from} limits the business ceded — only the treaty's terms apply.`);
      if (r.found) setFilter("wording");
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  // The input columns a rule reads — as the broker list names the column
  // each rule checks.
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const inputCols: { name: string; field: string | null }[] = own
    ? own.columns.map(c => ({ name: c.name, field: c.field }))
    : rec.options.inputs.filter(t => !t.own).flatMap(t => t.columns.map(c => ({ name: c.name, field: c.field })));
  const colsOf = (r: TreatyRule) => {
    const fields = rec.options.rule_fields?.[r.subject] ?? [];
    return [...new Set(inputCols.filter(c => c.field && fields.includes(c.field)).map(c => c.name))];
  };

  const terms = rules.filter(r => r.source !== "wording");
  const wording = rules.filter(r => r.source === "wording");
  const pool = filter === "terms" ? terms : filter === "wording" ? wording : rules;
  const listed = pool.filter(r => !q.trim()
    || `${r.label} ${colsOf(r).join(" ")}`.toLowerCase().includes(q.trim().toLowerCase()));

  const chip = (key: typeof filter, label: string, n: number) => (
    <button type="button" onClick={() => setFilter(key)} aria-pressed={filter === key}
      className={`rounded-full border px-3 py-1 text-xs font-semibold transition-colors
        ${filter === key ? "border-navy bg-navy/10 text-navy" : "border-border bg-white text-ink-muted hover:text-ink"}`}>
      {label} · {n}
    </button>
  );
  const sevTone = (sev: string) => sev === "critical"
    ? "bg-danger/10 text-danger" : "bg-amber-50 text-amber-700";

  return (
    <div>
      {!readOnly && (
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <p className="m-0 flex-1 text-xs text-ink-muted" style={{ minWidth: 300 }}>
          What every reinsurance bordereau for this treaty is checked against, as a
          broker&rsquo;s bordereau is checked against its contract. <b>Critical</b> stops a
          statement being sent until it is dealt with; <b>Warning</b> is shown and sent anyway.
        </p>
        <button className="btn" type="button" disabled={busy} onClick={read}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Sparkles size={14} />}
          {wording.length ? "Read the wording again" : "Read rules from the treaty wording"}
        </button>
      </div>
      )}
      {err && <div className="note warn" style={{ marginBottom: 10 }}>{err}</div>}
      {note && <div className="note ok" style={{ marginBottom: 10 }}>{note}</div>}

      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {chip("terms", "Treaty Terms", terms.length)}
        {chip("wording", "Treaty Wording", wording.length)}
        {chip("all", "All rules", rules.length)}
        <label className="ml-auto flex w-56 max-w-full items-center gap-1.5 rounded-md border border-border bg-white px-2 py-1">
          <Search size={12} className="shrink-0 text-ink-soft" />
          <input value={q} onChange={e => setQ(e.target.value)}
            placeholder="Search rules or columns" aria-label="Search rules or columns"
            className="w-full border-0 bg-transparent text-xs outline-none"
            style={{ padding: 0, border: 0, boxShadow: "none" }} />
        </label>
      </div>

      <div className="overflow-hidden rounded-md border border-border">
        {listed.length === 0 && (
          <p className="px-3 py-3 text-xs text-ink-muted" style={{ margin: 0 }}>
            {q ? "No rule matches that."
              : filter === "wording" ? "None yet — read them from the treaty wording above."
              : "No rules in this group."}
          </p>
        )}
        {listed.map(r => {
          const isOpen = open === r.id;
          const cols = colsOf(r);
          return (
            <div key={r.id} className="border-b border-border last:border-b-0">
              <button type="button" aria-expanded={isOpen}
                onClick={() => setOpen(isOpen ? null : r.id)}
                className={`grid w-full grid-cols-[minmax(0,1fr)_auto_auto_14px] items-center gap-3 px-3 py-2.5 text-left transition-colors
                  ${isOpen ? "bg-surface-2" : "bg-white hover:bg-surface-2"}`}
                style={{ border: 0, borderRadius: 0, opacity: r.enabled ? 1 : .55 }}>
                <span className="min-w-0">
                  <span className="flex flex-wrap items-center gap-1.5 text-[13px] font-semibold text-ink">
                    {r.label}
                    {!r.enabled && <span className="rounded-full bg-surface-2 px-1.5 text-[10px] font-bold text-ink-muted">Off</span>}
                  </span>
                  <span className="block truncate text-[11px] text-ink-muted">
                    {r.source === "wording" ? "From the treaty wording" : "From the treaty terms"}
                  </span>
                </span>
                <span className="hidden max-w-[16rem] flex-wrap justify-end gap-1 sm:flex">
                  {cols.slice(0, 2).map(c => (
                    <span key={c} className="truncate rounded-full bg-navy/10 px-2 py-0.5 font-mono text-[10.5px] font-semibold text-navy">{c}</span>
                  ))}
                  {cols.length > 2 && <span className="text-[11px] text-ink-muted">+{cols.length - 2}</span>}
                </span>
                <span className={`rounded-full px-2 py-0.5 text-[10.5px] font-bold ${sevTone(r.severity)}`}>
                  {r.severity === "critical" ? "Critical" : "Warning"}
                </span>
                {isOpen ? <ChevronDown size={14} className="text-ink-soft" /> : <ChevronRight size={14} className="text-ink-soft" />}
              </button>
              {isOpen && (
                <div className="space-y-2 bg-surface-2 px-3 pb-3">
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-1 text-[11px] text-ink-muted">
                    <span className="font-semibold uppercase tracking-wide text-ink-soft">Input column it checks</span>
                    {cols.length ? cols.map(c => (
                      <span key={c} className="rounded-full bg-navy/10 px-2 py-0.5 font-mono text-[10.5px] font-semibold text-navy">{c}</span>
                    )) : (
                      <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-700">
                        No input column carries it — not tested
                      </span>
                    )}
                  </div>
                  <div className="rounded-md bg-white p-3 text-[12.5px]">
                    {r.clause ? (
                      <div className="mb-3 flex gap-1.5 text-ink-muted">
                        <ScrollText size={13} className="mt-0.5 shrink-0" />
                        <i>&ldquo;{r.clause}&rdquo;</i>
                      </div>
                    ) : (
                      <div className="mb-3 text-ink-muted">Worked out from the treaty&rsquo;s terms — it follows them if they change.</div>
                    )}
                    {readOnly ? (
                      <div className="text-ink-muted">
                        {r.enabled ? <>Checked · <b>{r.severity === "critical" ? "Critical" : "Warning"}</b> if broken</> : "Switched off"}
                      </div>
                    ) : (
                    <div className="flex flex-wrap items-center gap-4">
                      <label className="flex items-center gap-2 font-medium" style={{ cursor: "pointer" }}>
                        <input type="checkbox" checked={r.enabled} style={{ width: "auto" }}
                               onChange={e => set(r.id, { enabled: e.target.checked })} />
                        Check this rule
                      </label>
                      <label className="flex items-center gap-2 font-medium">
                        If broken
                        <select value={r.severity} disabled={!r.enabled} style={{ width: "auto" }}
                                onChange={e => set(r.id, { severity: e.target.value as TreatyRule["severity"] })}>
                          <option value="critical">Critical — stops sending</option>
                          <option value="warning">Warning — sent anyway</option>
                        </select>
                      </label>
                    </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** The treaty's rules a statement's rows broke — on Check and on the
 *  statement itself. */
export function TreatyChecks({ checks }: { checks?: TreatyCheck[] }) {
  // Statements checked before the rename still say "blocking".
  const broken = (checks ?? []).filter(c => c.severity !== "untested")
    .map(c => (c.severity as string) === "blocking" ? { ...c, severity: "critical" as const } : c);
  const untested = (checks ?? []).filter(c => c.severity === "untested");
  return (
    <div className="card" style={{ marginBottom: 18 }}>
      <div className="card-h">
        {broken.some(c => c.severity === "critical")
          ? <AlertTriangle size={16} className="ci" style={{ color: "var(--p-crit-ink)" }} />
          : <CheckCircle2 size={16} className="ci" />}
        <h3>Treaty checks</h3>
        <span className="sub">
          {broken.length === 0 ? "every row meets the treaty's rules"
            : `${broken.length} rule${broken.length === 1 ? "" : "s"} broken`}
        </span>
      </div>
      {broken.length > 0 && (
        <div className="tbl-wrap">
          <table className="panel-tbl">
            <thead><tr><th style={{ width: 110 }}>Severity</th><th>Rule</th><th style={{ width: 80 }}>Rows</th><th>For example</th></tr></thead>
            <tbody>
              {broken.map(c => (
                <tr key={c.id}>
                  <td><span className={`badge ${c.severity === "critical" ? "b-crit" : "b-warn"}`}>
                    <span className="d" />{c.severity === "critical" ? "Critical" : "Warning"}</span></td>
                  <td>
                    <b style={{ fontWeight: 600 }}>{c.label}</b>
                    {c.clause && <div className="sub"><i>&ldquo;{c.clause}&rdquo;</i></div>}
                  </td>
                  <td className="mono">{c.rows}</td>
                  <td className="sub">
                    {c.examples.slice(0, 3).map((x, i) => (
                      <div key={i}>{x.policy ? `${x.policy}: ` : ""}{x.why}</div>
                    ))}
                    {c.rows > 3 && <div>+{c.rows - 3} more</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {untested.length > 0 && (
        <div className="sub" style={{ padding: "10px 20px" }}>
          Not tested — the bordereau does not carry what they need:{" "}
          {untested.map(c => c.label).join("; ")}.
        </div>
      )}
    </div>
  );
}
