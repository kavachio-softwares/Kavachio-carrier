/**
 * One treaty's reinsurance bordereau setup — laid out like a broker's
 * Bordereau Setup.
 *
 * A NEW setup is three steps, as the broker's:
 *   1 Setup Details         the treaty and the programmes that feed it
 *   2 Documents             the input template, the output template and the
 *                           signed wording — then the input columns
 *   3 Output BDX Template   the file each reinsurer receives
 * and "Set Up Bordereau Pipeline" at the foot saves it with the mapping and
 * the treaty's rules worked out, then offers Review Mapping or Activate.
 *
 * A SAVED setup is edited on the broker Edit's tabs: Treaty Rules, Map Input &
 * Output Columns, Output BDX Template and Due Dates (schedule and recipients),
 * with Activate / Save Draft held at the foot of the screen.
 */
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle, ArrowLeft, ArrowRight, CalendarDays, CheckCircle2, Columns3, FileSpreadsheet,
  FileText, Info, ListChecks, Save, Send, ShieldCheck, Trash2, Unlink,
} from "lucide-react";
import { Modal } from "../components/ui/Modal";
import OutputGrid from "../components/treaty/OutputGrid";
import { Stat } from "../components/treaty/SetupTiles";
import { PageBody, PageHeader } from "../components/Layout";
import Card from "../components/ui/Card";
import Button from "../components/ui/Button";
import { SetupTabs, useSetupTab, type SetupTab } from "../components/SetupTabs";
import { InfoTip } from "../components/InfoTip";
import CededLayoutSection from "../components/treaty/CededLayoutSection";
import {
  FieldMappingStep, InputTemplateStep, treatyRuleLine, unmatchedOutputs,
} from "../components/treaty/CededMappingSteps";
import CededDocumentsStep from "../components/treaty/CededDocumentsStep";
import { TreatyRulesStep } from "../components/treaty/TreatyRules";
import { fieldErrors } from "../api/contractRecord";
import {
  activateSetup, deleteSetup, getSetup, previewSetup, saveSetup, sendBackSetup,
  type SetupPreview, type SetupRecord, type SetupValues,
} from "../api/ceded";
import { fmtStamp } from "../utils/date";
import { SETUP_STATUS } from "./ReinsuranceSetups";

const FREQ = [
  { key: "monthly", label: "Monthly" },
  { key: "quarterly", label: "Quarterly" },
  { key: "annually", label: "Yearly" },
] as const;

const TAB_KEYS: SetupTab[] = [
  { key: "details", label: "" }, { key: "documents", label: "" }, { key: "output", label: "" },
];
// A saved setup's tabs, in the broker Edit's order.
const EDIT_KEYS: SetupTab[] = [
  { key: "rules", label: "" }, { key: "mapping", label: "" }, { key: "output", label: "" },
  { key: "calendar", label: "" },
];

export default function ReinsuranceSetupEdit() {
  const { contractId } = useParams();
  const cid = Number(contractId);
  const [rec, setRec] = useState<SetupRecord | null>(null);
  const [v, setV] = useState<SetupValues | null>(null);
  const [ccText, setCcText] = useState("");
  const [dirty, setDirty] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [pv, setPv] = useState<SetupPreview | null>(null);
  const [backNote, setBackNote] = useState("");
  const [showBack, setShowBack] = useState(false);
  const nav = useNavigate();
  // The open step lives in ?tab=, as on the broker setup.
  const [saved, setSaved] = useState<boolean | null>(null);
  const [tab, setTab] = useSetupTab(saved ? EDIT_KEYS : TAB_KEYS);
  const [deleting, setDeleting] = useState(false);
  // The output's column controls, opened from the grid as the broker's are.
  const [columnsOpen, setColumnsOpen] = useState(false);
  // What "Set Up Bordereau Pipeline" made — the broker's "Mapping Generated".
  const [built, setBuilt] = useState<{ columns: number; matched: number; rules: number } | null>(null);

  function load(r: SetupRecord) {
    setRec(r);
    setSaved(!!r.setup);
    // A new setup starts with no output template: it is uploaded or created
    // on Documents, as on the broker setup — not built behind the user's back.
    const vals = r.setup ?? { ...r.defaults, layout: { source: "contract" as const }, columns: [] };
    setV({ ...vals, program_ids: vals.program_ids ?? r.options.programmes.map(p => p.id) });
    setCcText((vals.cc ?? []).join(", "));
    setDirty(false);
  }
  useEffect(() => {
    getSetup(cid).then(load).catch(e => setErr(fieldErrors(e).message));
  }, [cid]);

  const values = useMemo<SetupValues | null>(() => v && ({
    ...v, cc: ccText.split(/[,;\s]+/).map(x => x.trim()).filter(Boolean),
  }), [v, ccText]);

  // Worked out in the background as the settings are typed: it gives Field
  // Mapping its Example column and catches a setting that would not save.
  // There is no Preview step — Check on Reinsurance Bordereaux does that.
  useEffect(() => {
    if (!values || !rec?.has_structure) return;
    // A layout of the carrier's own is previewed once it has been read.
    if (values.layout && values.layout.source !== "kavachio" && !values.columns.length) return;
    const t = setTimeout(() => {
      previewSetup(cid, values).then(p => { setPv(p); setErrors({}); })
        .catch(e => setErrors(fieldErrors(e).errors as Record<string, string>));
    }, 400);
    return () => clearTimeout(t);
  }, [values, cid, rec?.has_structure]);

  function change(patch: Partial<SetupValues>) {
    setV(cur => cur && { ...cur, ...patch });
    setDirty(true);
  }

  async function act(label: string, fn: () => Promise<SetupRecord>, ok: string) {
    setBusy(label); setErr(""); setNote(""); setErrors({});
    try {
      load(await fn());
      setNote(ok);
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(fe.message);
      setErrors(fe.errors as Record<string, string>);
    } finally {
      setBusy("");
    }
  }

  if (!rec || !v || !values) {
    return (
      <div className="proto"><div className="view full">
        {err ? <div className="note warn">{err}</div> : <div className="sub">Loading…</div>}
      </div></div>
    );
  }

  const status = rec.setup?.status ?? "none";
  const st = SETUP_STATUS[status];
  const bad = (k: string) => errors[k]
    ? <div className="hint" style={{ color: "var(--p-crit-ink)" }}>{errors[k]}</div> : null;

  const save = () => act("save", () => saveSetup(cid, values),
    "Saved as a draft. Activate it to start preparing statements with it.");
  const activate = () => act("activate", async () => {
    if (dirty || !rec.setup) await saveSetup(cid, values);
    return activateSetup(cid);
  }, rec.carrier_admin
    ? "Active. Statements for this treaty are now prepared every period."
    : "Sent to your carrier admin to approve.");

  const detailsBody = (<>
            <p className="sub" style={{ marginBottom: 14 }}>
              The treaty this setup is for, and the programmes whose bordereaux make up
              its statements.
            </p>
            <div className="grid g-2" style={{ gap: 14, marginBottom: 18 }}>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Treaty</label>
                <div className="ro" style={{ padding: "9px 12px", border: "1px solid var(--p-border)",
                     borderRadius: 8, background: "var(--p-surface-2, #f7f8fa)" }}>
                  <b>{rec.treaty}</b>{rec.type_label ? <span className="sub"> · {rec.type_label}</span> : null}
                </div>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Status</label>
                <div style={{ padding: "9px 0" }}>
                  <span className={`badge ${st.cls}`}><span className="d" />{st.label}</span>
                  {rec.setup?.updated_at && <span className="sub"> · saved {fmtStamp(rec.setup.updated_at)}</span>}
                </div>
              </div>
            </div>
            <div className="fh" style={{ marginTop: 0 }}>
              Programmes that feed it
              <InfoTip text="The bordereaux of these programmes make up each statement. Only programmes this treaty protects can be chosen." />
            </div>
              {/* Each programme with what it holds — its treaties (this one among
                  them) and its broker contracts — so it is clear what feeds it. */}
              <div className="grid g-2" style={{ gap: 10 }}>
                {rec.options.programmes.map(p => {
                  const on = (v.program_ids ?? []).includes(p.id);
                  const treaties = (rec.options.treaties ?? []).filter(t => t.program_ids.includes(p.id));
                  const contracts = (rec.options.contracts ?? []).filter(k => k.program_id === p.id);
                  return (
                    <label key={p.id} style={{ display: "block", cursor: "pointer", padding: "10px 12px",
                             border: `1px solid ${on ? "var(--p-ok, #0f766e)" : "var(--p-border-2)"}`,
                             borderRadius: 10, background: on ? "var(--p-ok-bg, #f0fdf9)" : "var(--p-surface)" }}>
                      <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        <input type="checkbox" checked={on} style={{ margin: 0 }}
                               onChange={e => change({ program_ids: e.target.checked
                                 ? [...(v.program_ids ?? []), p.id]
                                 : (v.program_ids ?? []).filter(x => x !== p.id) })} />
                        <b style={{ fontSize: 13 }}>{p.name}</b>
                        {p.is_home && <span className="badge b-mut" style={{ fontSize: 10 }}>Home programme</span>}
                      </span>
                      <span style={{ display: "block", marginTop: 8, paddingLeft: 22, fontSize: 12.5 }}>
                        <span className="sub" style={{ display: "block", fontSize: 11, textTransform: "uppercase",
                              letterSpacing: ".03em", marginBottom: 3 }}>
                          Treaties ({treaties.length})
                        </span>
                        {treaties.length === 0 && <span className="sub">None</span>}
                        {treaties.map(t => (
                          <span key={t.id} style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }}>
                            <ShieldCheck size={12} style={{ alignSelf: "center", flexShrink: 0 }} />
                            {t.this ? <b>{t.name}</b>
                              : <Link className="linkish" to={`/contracts/${t.id}`}
                                      onClick={e => e.stopPropagation()}>{t.name}</Link>}
                            <span className="sub">
                              {[t.type_label, t.status && t.status[0].toUpperCase() + t.status.slice(1)]
                                .filter(Boolean).join(" · ")}
                            </span>
                            {t.this && <span className="badge b-ok" style={{ fontSize: 10 }}>This treaty</span>}
                          </span>
                        ))}
                        <span className="sub" style={{ display: "block", fontSize: 11, textTransform: "uppercase",
                              letterSpacing: ".03em", margin: "8px 0 3px" }}>
                          Contracts ({contracts.length})
                        </span>
                        {contracts.length === 0 && <span className="sub">None</span>}
                        {contracts.map(k => (
                          <span key={k.id} style={{ display: "flex", gap: 6, alignItems: "baseline", flexWrap: "wrap" }}>
                            <FileText size={12} style={{ alignSelf: "center", flexShrink: 0 }} />
                            <Link className="linkish" to={`/contracts/${k.id}`}
                                  onClick={e => e.stopPropagation()}>{k.name}</Link>
                            <span className="sub">
                              {[k.broker, k.status && k.status[0].toUpperCase() + k.status.slice(1)]
                                .filter(Boolean).join(" · ")}
                            </span>
                          </span>
                        ))}
                      </span>
                    </label>
                  );
                })}
              </div>
              {bad("program_ids")}
          </>);
  const scheduleBody = (<>
            <div className="fh" style={{ marginTop: 24 }}>Schedule</div>
              <div className="grid g-2" style={{ gap: 14 }}>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>
                    One statement every
                    <InfoTip text="Monthly bordereaux are grouped into the quarter or year when the statement covers a longer period." />
                  </label>
                  <div className="seg">
                    {FREQ.map(f => (
                      <button key={f.key} type="button"
                              className={v.frequency === f.key ? "on" : ""}
                              onClick={() => change({ frequency: f.key })}>{f.label}</button>
                    ))}
                  </div>
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Due, days after the period ends</label>
                  <input type="number" min={0} max={365} value={v.due_days}
                         onChange={e => change({ due_days: Number(e.target.value) })} />
                  {bad("due_days")}
                </div>
              </div>
          </>);
  const recipientsBody = (<>
            <p className="sub" style={{ marginBottom: 14 }}>
              Who receives each statement, who is copied, and whether it goes out on
              its own.
            </p>
            <div className="fh" style={{ marginTop: 0 }}>
              Who receives it
              <InfoTip text="Each reinsurer is sent only its own statement. Tick nobody and every contact with an email receives it." />
            </div>
              {rec.options.reinsurers.length === 0 && (
                <div className="sub">No reinsurer is on this treaty&rsquo;s panel yet.</div>
              )}
              {rec.options.reinsurers.map(r => {
                const chosen = v.recipients[String(r.party_id)] ?? [];
                return (
                  <div key={r.party_id} style={{ marginBottom: 12 }}>
                    <b style={{ fontSize: 13 }}>{r.name}</b>{" "}
                    <span className="sub">{chosen.length ? `${chosen.length} chosen` : "every contact"}</span>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 6 }}>
                      {r.contacts.length === 0 && (
                        <span className="sub">
                          No contacts — <Link className="linkish" to={`/parties/${r.party_id}`}>add one</Link>,
                          or the statement is on its private link only.
                        </span>
                      )}
                      {r.contacts.map(ct => {
                        const on = chosen.includes(ct.id);
                        return (
                          <label key={ct.id} className={`badge ${on ? "b-ok" : ""}`}
                                 style={{ border: "1px solid var(--p-border-2)", padding: "5px 10px",
                                          fontSize: 12.5, cursor: ct.email ? "pointer" : "not-allowed",
                                          opacity: ct.email ? 1 : .55,
                                          background: on ? undefined : "var(--p-surface)" }}>
                            <input type="checkbox" checked={on} disabled={!ct.email} style={{ margin: 0 }}
                                   onChange={e => change({ recipients: { ...v.recipients,
                                     [String(r.party_id)]: e.target.checked
                                       ? [...chosen, ct.id] : chosen.filter(x => x !== ct.id) } })} />
                            {ct.name} <span className="sub">{ct.email ?? "no email"}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
              <div className="field" style={{ marginBottom: 0, marginTop: 6 }}>
                <label>Copy to your team <span className="muted" style={{ fontWeight: 500 }}>— optional</span></label>
                <input value={ccText} placeholder="finance@yourcompany.com, re-team@yourcompany.com"
                       onChange={e => { setCcText(e.target.value); setDirty(true); }} />
                {bad("cc")}
              </div>
            <div className="fh" style={{ marginTop: 24 }}>Sending</div>
              <div className="startpick" style={{ gridTemplateColumns: "repeat(2, minmax(0,1fr))" }}>
                <div className={`sp ${v.send_mode === "manual" ? "on" : ""}`}
                     onClick={() => change({ send_mode: "manual" })}>
                  <b>When I press Send</b>
                  <span>Each statement is prepared as a draft. You check it and send it.</span>
                </div>
                <div className={`sp ${v.send_mode === "auto" ? "on" : ""}`}
                     onClick={() => change({ send_mode: "auto" })}>
                  <b>Automatically</b>
                  <span>Sent as soon as every programme&rsquo;s bordereau for the period is in.</span>
                </div>
              </div>
          </>);

  // A saved setup is EDITED as the broker setup's Edit screen is: the same
  // tabs, the same tables. A setup not saved yet is built step by step.
  const editing = !!rec.setup;
  // What the build needs, as the broker's "Still needed" names it.
  const programmesOk = (v.program_ids ?? []).length > 0;
  const inputOk = v.input_layout?.source === "own"
    ? v.input_layout.columns.length > 0
    : rec.options.inputs.some(t => !t.own);
  const outputOk = (v.layout?.source ?? "kavachio") !== "kavachio"
    && v.columns.some(c => c.keep !== false);
  const missing: { tab: string; what: string }[] = [
    ...(!rec.has_structure ? [{ tab: "details", what: "the treaty's structure" }] : []),
    ...(!programmesOk ? [{ tab: "details", what: "a programme that feeds it" }] : []),
    ...(!rec.wording ? [{ tab: "documents", what: "the signed wording" }] : []),
    ...(!inputOk ? [{ tab: "documents", what: "the input template" }] : []),
    ...(!outputOk ? [{ tab: "output", what: "the output template" }] : []),
  ];
  // Only what can be done on THIS step — as on the broker setup.
  const missingHere = missing.filter(m => tab === "output" || m.tab === tab
    || (tab === "documents" && m.tab === "details")).map(m => m.what);
  const unmatched = unmatchedOutputs(rec, v);

  async function buildSetup() {
    setBusy("build"); setErr(""); setNote(""); setErrors({});
    try {
      const r = await saveSetup(cid, values!);
      const sv = r.setup ?? r.defaults;
      const cols = sv.columns.filter(c => c.keep !== false);
      setBuilt({ columns: cols.length,
                 matched: cols.length - unmatchedOutputs(r, sv).length,
                 rules: (sv.rules ?? []).filter(x => x.enabled).length });
      load(r);
    } catch (e) {
      const fe = fieldErrors(e);
      setErr(fe.message);
      setErrors(fe.errors as Record<string, string>);
    } finally {
      setBusy("");
    }
  }
  const tabs: SetupTab[] = editing ? [
    { key: "rules", label: "Treaty Rules",
      count: (v.rules ?? []).filter(r => r.enabled).length || undefined,
      info: "What every reinsurance bordereau for this treaty is checked against." },
    { key: "mapping", label: "Map Input & Output Columns",
      warn: !!errors.input_layout || (v.input_layout?.source !== "own" && rec.options.inputs.length === 0),
      info: "The input this treaty is read from, and the input column each output column comes from." },
    { key: "output", label: "Output BDX Template", warn: !!errors.columns || !!errors.layout,
      info: "The file each reinsurer receives." },
    { key: "calendar", label: "Due Dates", warn: !!errors.cc,
      info: "Which programmes feed it, how often a statement is made, when it is due, and who receives it." },
  ] : [
    { key: "details", label: "Setup Details", step: 1, warn: !!errors.program_ids },
    { key: "documents", label: "Documents", step: 2,
      warn: !!errors.input_layout || (!!programmesOk && !inputOk),
      disabled: !programmesOk, disabledReason: "Choose the programmes that feed it first" },
    { key: "output", label: "Output BDX Template", step: 3,
      warn: !!errors.columns || !!errors.layout,
      disabled: !programmesOk || !inputOk,
      disabledReason: !programmesOk ? "Choose the programmes that feed it first"
        : "Add the input template on Documents first" },
  ];
  const nextTab = editing ? undefined : tabs[tabs.findIndex(t => t.key === tab) + 1];

  // ── a saved setup, laid out as the broker setup's Edit ──
  const enabledRules = (v.rules ?? []).filter(r => r.enabled);
  const fieldsIn = new Set((v.input_layout?.source === "own" ? v.input_layout.columns
    : rec.options.inputs.filter(t => !t.own).flatMap(t => t.columns)).map(c => c.field).filter(Boolean) as string[]);
  const untested = enabledRules.filter(r => !(rec.options.rule_fields?.[r.subject] ?? []).some(f => fieldsIn.has(f)));
  const critical = enabledRules.filter(r => r.severity === "critical").length;
  const lay = v.layout ?? { source: "kavachio" as const };
  const outputName = lay.source === "uploaded" ? (lay.filename ?? "Your output template")
    : lay.source === "standard" ? `Lloyd's risk sheet${lay.standard?.jurisdiction ? ` (${lay.standard.jurisdiction})` : ""}`
    : lay.source === "contract" ? "Built from the treaty's terms"
    : "Kavachio standard layout";
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const inputName = own ? (own.filename ?? "Your input template") : "Your brokers' bordereaux";
  const kept = v.columns.filter(c => c.keep !== false);
  const editLayout = (
    <>
      <SetupTabs tabs={tabs} current={tab} onChange={setTab} />
      {/* The wizard's own parts keep their .proto look; the broker-style
          cards and the spreadsheet grid sit outside it. */}
      <div className="space-y-4">
        {tab === "rules" && (
          <Card title={<span className="flex items-center gap-2">
            <ListChecks size={16} className="text-navy" /> Treaty Rules
            <span className="text-xs font-normal text-ink-muted">· {enabledRules.length} rule(s) checked on every reinsurance bordereau</span>
          </span>}>
            <div className="mb-4 grid gap-3 sm:grid-cols-4">
              <Stat icon={ListChecks} n={enabledRules.length - untested.length} label="Rules check your file" />
              <Stat icon={Unlink} n={untested.length} label="Rules with no column" warn={untested.length > 0} />
              <Stat icon={AlertTriangle} n={critical} label="Critical — stop sending" />
              <Stat icon={Info} n={enabledRules.length - critical} label="Warning — sent anyway" />
            </div>
            <div className="proto" style={{ background: "transparent" }}>
              <TreatyRulesStep cid={cid} rec={rec} v={v} change={change} />
            </div>
          </Card>
        )}
        {tab === "mapping" && (
          <>
            <Card title={<span className="flex items-center gap-2">
              <FileSpreadsheet size={16} className="text-navy" /> Sheet Mapping</span>}>
              <p className="mb-3 text-[12.5px] text-ink-muted">
                Each output sheet: which input feeds it and which treaty checks it.
              </p>
              <div className="overflow-hidden rounded-md border border-border">
                <div className="grid grid-cols-[1fr_1.4fr_1fr] gap-3 bg-surface-2 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
                  <span>Output sheet</span><span>Input</span><span>Treaty</span>
                </div>
                <div className="grid grid-cols-[1fr_1.4fr_1fr] items-center gap-3 border-t border-border bg-white px-3 py-2.5 text-[13px]">
                  <span>{lay.sheet_name || "Lines"}</span>
                  <span className="flex flex-wrap gap-1.5">
                    {(own ? [own.filename ?? "Your input template"]
                      : rec.options.inputs.filter(t => !t.own).map(t => t.programme ?? t.name)).map(n => (
                      <span key={n} className="rounded bg-surface-2 px-2 py-0.5 font-mono text-[11.5px] text-ink">{n}</span>
                    ))}
                  </span>
                  <span className="truncate text-ink-muted">{rec.treaty}</span>
                </div>
              </div>
              <div className="proto mt-3" style={{ background: "transparent" }}>
                <InputTemplateStep cid={cid} rec={rec} v={v} change={change} bad={bad} noColumns />
              </div>
            </Card>
            <p className="flex items-center gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-ink-muted">
              <CheckCircle2 size={14} className="shrink-0" /> {treatyRuleLine(rec, v)}
            </p>
            <Card title="Field Mapping & Validation Rules">
              <div className="proto" style={{ background: "transparent" }}>
                <FieldMappingStep rec={rec} v={v} pv={pv} change={change} hideStrip />
              </div>
            </Card>
          </>
        )}
        {tab === "output" && (
          <Card title={<span className="flex items-center gap-2">
            <FileSpreadsheet size={16} className="text-navy" /> Output BDX · {outputName}
            <span className="text-xs font-normal text-ink-muted">{v.file_format === "csv" ? "CSV" : "Excel"}</span>
          </span>}>
            <p className="mb-3 text-[12.5px] text-ink-muted">
              The file each reinsurer receives — its share of every line, in delivery order,
              under the names the file will carry.
            </p>
            {/* Sample values matched by heading, not position — a column just
                moved, added or removed never shows another column's values
                while the preview catches up. */}
            <OutputGrid headings={kept.map(c => c.heading)}
              rows={(pv?.table?.rows ?? []).slice(0, 5).map(r => kept.map(c => {
                const j = pv?.table?.headings.indexOf(c.heading) ?? -1;
                return j >= 0 ? r[j] : null;
              }))}
              sheet={lay.sheet_name || "Lines"} period={pv?.period ?? null}
              footer={<>
                <button type="button" onClick={() => setColumnsOpen(o => !o)}
                  className="rounded border border-border bg-white px-2.5 py-1 text-[12px] font-medium text-ink hover:border-navy">
                  {columnsOpen ? "Done" : "+ Add column"}
                </button>
                <span className="text-[11.5px] text-ink-muted">
                  {columnsOpen ? "" : "or change, rename, reorder and remove columns, or the layout"}
                </span>
              </>} />
            {columnsOpen && (
              <div className="proto" style={{ marginTop: 20, background: "transparent" }}>
                <CededLayoutSection cid={cid} rec={rec} v={v} change={change} bad={bad} />
              </div>
            )}
          </Card>
        )}
        {tab === "calendar" && (
          <Card title={<span className="flex items-center gap-2">
            <CalendarDays size={16} className="text-navy" /> Due Dates</span>}>
            <div className="mb-4 rounded-lg border border-emerald-200 bg-emerald-50/50 px-4 py-3">
              <div className="mb-2 text-[13px] font-semibold text-emerald-800">Statements are set for this treaty</div>
              <div className="grid grid-cols-2 gap-3 text-[12.5px] md:grid-cols-4">
                <div><div className="text-ink-muted">How often</div><b>{FREQ.find(f => f.key === v.frequency)?.label}</b></div>
                <div><div className="text-ink-muted">Due</div><b>{v.due_days} days after the period</b></div>
                <div><div className="text-ink-muted">Programmes feeding it</div>
                  <b>{(v.program_ids ?? []).length} of {rec.options.programmes.length}</b></div>
                <div><div className="text-ink-muted">Sending</div>
                  <b>{v.send_mode === "auto" ? "Automatically" : "When you press Send"}</b></div>
              </div>
            </div>
            <div className="proto" style={{ background: "transparent" }}>{detailsBody}{scheduleBody}</div>
          </Card>
        )}
        {tab === "calendar" && (
          <Card title={<span className="flex items-center gap-2">
            <Send size={16} className="text-navy" /> Recipients &amp; Sending</span>}>
            <div className="proto" style={{ background: "transparent" }}>{recipientsBody}</div>
          </Card>
        )}
      </div>
    </>
  );

  return (
    <>
      <PageHeader title={editing ? `${rec.treaty} — Reinsurance bordereau` : "Reinsurance Bordereau Setup"}
        subtitle={editing
          ? `${SETUP_STATUS[status]?.label ?? status} · Last Modified ${fmtStamp(rec.setup?.updated_at)}`
          : "Set up once per treaty — then every period each reinsurer gets its own statement, worked out from your brokers' bordereaux."}
        action={rec.setup ? (
          // A saved setup is read on its view page; Edit came from there.
          <Button variant="secondary" onClick={() => nav(`/reinsurance/setups/${cid}?tab=${tab}`)}>
            <ArrowLeft size={15} /> Back to setup
          </Button>
        ) : (
          <Button variant="secondary" onClick={() => nav("/direct/setups?tab=reinsurance")}>
            <ArrowLeft size={15} /> All setups
          </Button>
        )} />
      <PageBody>
        {(!rec.has_structure || !rec.wording || (rec.setup?.note && status === "draft") || (status === "active" && dirty)
          || (status === "pending_approval" && rec.carrier_admin) || err || note) && (
        <div className="proto" style={{ background: "transparent" }}>
        {!rec.has_structure && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            This treaty has no structure yet, so nothing can be ceded under it.{" "}
            <Link className="linkish" to={`/contracts/${cid}?tab=treaty`}>Set out its structure</Link>
          </div>
        )}
        {/* 26.3 — a treaty is configured from its signed wording. */}
        {!rec.wording && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            <b>Signed wording needed.</b> This treaty is configured from its signed
            wording, and none has been supplied — so this setup cannot be saved or
            activated yet. Attach the signed copy on the treaty (Documents → Wording,
            &ldquo;This is the signed copy&rdquo;), or have the treaty signed in Kavachio.{" "}
            <Link className="linkish" to={`/contracts/${cid}`}>Open the treaty</Link>
          </div>
        )}
        {rec.setup?.note && status === "draft" && (
          <div className="note warn" style={{ marginBottom: 14 }}>
            <b>Sent back:</b> {rec.setup.note}
          </div>
        )}
        {status === "active" && dirty && (
          <div className="note" style={{ marginBottom: 14 }}>
            Changes to an active setup take effect once it is activated again.
          </div>
        )}
        {status === "pending_approval" && rec.carrier_admin && (
          <div className="note" style={{ marginBottom: 14 }}>
            <b>Waiting for your approval.</b> Check the settings and the examples on Field Mapping,
            then activate it — or send it back.{" "}
            <button className="btn sm" type="button" onClick={() => setShowBack(s => !s)}>
              Send back
            </button>
            {showBack && (
              <div className="rowacts">
                <input style={{ flex: 1, minWidth: 240 }} value={backNote}
                       onChange={e => setBackNote(e.target.value)}
                       placeholder="What needs to change?" />
                <button className="btn sm" type="button" disabled={!backNote.trim() || !!busy}
                        onClick={() => act("back", () => sendBackSetup(cid, backNote.trim()),
                          "Sent back with your note.")}>Send back</button>
              </div>
            )}
          </div>
        )}
        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}
        {note && <div className="note ok" style={{ marginBottom: 14 }}>{note}</div>}

        </div>
        )}

        {editing ? editLayout : (
        <Card>
          <SetupTabs tabs={tabs} current={tab} onChange={setTab} />
          <div className="proto" style={{ background: "transparent" }}>

          {/* ---- 1 Setup Details ---- */}
          <div role="tabpanel" aria-label="Setup Details" hidden={tab !== "details"}>
            {detailsBody}
          </div>

          {/* ---- 2 Documents ----
              The upload boxes — input, output, signed wording — then what the
              input gave, as the broker setup's Documents tab. */}
          <div role="tabpanel" aria-label="Documents" hidden={tab !== "documents"}>
            <CededDocumentsStep cid={cid} rec={rec} v={v} change={change}
              onOutputStep={inputOk ? () => setTab("output") : undefined} />

            <div className="fh" style={{ marginTop: 26 }}>
              Input columns
              <InfoTip text="What the reinsurance bordereau is read from: your own input template when you upload one, else your brokers' bordereaux on the programmes this treaty protects." />
            </div>
            <InputTemplateStep cid={cid} rec={rec} v={v} change={change} bad={bad} listOnly />
          </div>

          {/* ---- 3 Output BDX Template ---- */}
          <div role="tabpanel" aria-label="Output BDX Template" hidden={tab !== "output"}>
            <CededLayoutSection cid={cid} rec={rec} v={v} change={change} bad={bad} />
          </div>
          </div>

          {/* The action, and — when it is off — WHY, in words on the page; then
              the way forward by name. As the broker setup's footer. */}
          <div className="-mx-5 -mb-5 mt-6 flex flex-wrap items-center gap-x-4
            gap-y-3 rounded-b-lg border-t border-border bg-surface-2/60 px-5 py-4">
            <Button onClick={buildSetup} disabled={!!busy || missing.length > 0}
              title={missing.length ? `Still needed: ${missing.map(m => m.what).join("; ")}` : undefined}
              className="!px-5 !py-2.5 !text-[13.5px] !rounded-lg
                shadow-md hover:shadow-lg hover:brightness-110 transition disabled:hover:brightness-100
                disabled:shadow-none disabled:!opacity-100 disabled:!bg-surface-2
                disabled:!text-ink-soft disabled:ring-1 disabled:ring-inset disabled:ring-border">
              {busy === "build" ? "Setting up…" : "Set Up Bordereau Pipeline"}
            </Button>
            {!busy && missingHere.length > 0 && (
              <span className="inline-flex items-center gap-1.5 text-[12.5px] text-warn">
                <AlertTriangle size={14} className="shrink-0" />
                Still needed: {missingHere.join(", ")}
              </span>
            )}
            {nextTab && (
              <Button variant="secondary" className="ml-auto" disabled={nextTab.disabled}
                title={nextTab.disabled ? nextTab.disabledReason : undefined}
                onClick={() => setTab(nextTab.key)}>
                Next: {nextTab.label} <ArrowRight size={14} />
              </Button>
            )}
          </div>
        </Card>
        )}

        {/* Save / Activate, fixed at the bottom like the broker setup's bar —
            on a saved setup only; a new one is built from the footer above. */}
        {editing && <>
        <div className="fixed bottom-0 right-0 z-30 px-8 py-3 bg-white/95 backdrop-blur
          border-t border-border shadow-[0_-1px_8px_rgba(17,24,39,0.06)]"
          style={{ left: "var(--sidebar-w, 256px)" }}>
          <div className="flex flex-wrap items-center gap-2">
            {(status !== "active" || dirty) && (
              <Button onClick={activate} disabled={!!busy || !rec.has_structure || !rec.wording}
                title={!rec.wording ? rec.wording_needed : undefined}>
                {rec.carrier_admin
                  ? <><CheckCircle2 size={15} /> Activate Setup</>
                  : <><Send size={15} /> Send for Approval</>}
              </Button>
            )}
            <Button variant="secondary" onClick={save} disabled={!!busy || !rec.wording}
              title={!rec.wording ? rec.wording_needed : undefined}>
              <Save size={15} /> Save Draft
            </Button>
            {editing && rec.carrier_admin && (
              <Button variant="danger" disabled={!!busy || status === "active"}
                title={status === "active" ? "This setup is active — statements are prepared with it, so it is not deleted" : undefined}
                onClick={() => setDeleting(true)}>
                <Trash2 size={15} /> Delete Draft
              </Button>
            )}
            {status === "active" && !dirty && (
              <span className="text-sm text-ink-muted">
                <CheckCircle2 size={14} className="inline -mt-0.5 text-emerald-600" /> Active —
                statements are prepared every period.
              </span>
            )}
            {unmatched.length > 0 && (
              <button type="button" onClick={() => setTab("mapping")}
                title={unmatched.map(c => c.heading).join(", ")}
                className="ml-auto inline-flex items-center gap-1 text-xs text-amber-600 hover:underline">
                <AlertTriangle size={13} /> {unmatched.length} Output Column{unmatched.length === 1 ? "" : "s"} Not Matched · show me
              </button>
            )}
          </div>
        </div>
        <div style={{ height: 68 }} aria-hidden="true" />
        </>}
      </PageBody>
      <Modal open={!!built} size="md" title="Mapping Generated"
        onClose={() => setBuilt(null)}
        footer={<div className="flex items-center justify-end gap-2">
          <Button variant="secondary" onClick={() => {
            setBuilt(null);
            nav(`/reinsurance/setups/${cid}/edit?tab=mapping`);
          }}>Review Mapping</Button>
          {status === "active" ? (
            <span className="inline-flex items-center gap-1 px-2 text-sm font-medium text-emerald-700">
              <CheckCircle2 size={15} /> Active
            </span>
          ) : (
            <Button disabled={!!busy || !rec.wording} onClick={() => {
              setBuilt(null);
              act("activate", () => activateSetup(cid), rec.carrier_admin
                ? "Active. Statements for this treaty are now prepared every period."
                : "Sent to your carrier admin to approve.");
            }}>
              {rec.carrier_admin
                ? <><CheckCircle2 size={15} /> Activate Setup</>
                : <><Send size={15} /> Send for Approval</>}
            </Button>
          )}
        </div>}>
        {built && (
          <div className="space-y-3 text-sm">
            <p className="text-ink-muted">
              The setup for <b className="text-ink">{rec.treaty}</b> is saved as a draft, with
              each output column matched to the input column that fills it and the treaty&rsquo;s
              rules worked out from its terms.
            </p>
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-lg border border-border px-3 py-2">
                <div className="text-lg font-semibold">{built.matched}/{built.columns}</div>
                <div className="text-[11.5px] text-ink-muted">Output columns matched</div>
              </div>
              <div className="rounded-lg border border-border px-3 py-2">
                <div className="text-lg font-semibold">{built.rules}</div>
                <div className="text-[11.5px] text-ink-muted">Treaty rules</div>
              </div>
              <div className="rounded-lg border border-border px-3 py-2">
                <div className="text-lg font-semibold">{FREQ.find(f => f.key === v.frequency)?.label}</div>
                <div className="text-[11.5px] text-ink-muted">Due {v.due_days} days after</div>
              </div>
            </div>
            {built.matched < built.columns && (
              <div className="flex items-start gap-2 rounded-md bg-amber-50 px-3 py-2 text-[12.5px] text-amber-800">
                <AlertTriangle size={14} className="mt-0.5 shrink-0" />
                {built.columns - built.matched} output column(s) have no input column yet —
                they are blank until you match them on Review Mapping.
              </div>
            )}
            <p className="text-[12.5px] text-ink-muted">
              Schedule and recipients can be changed on the setup&rsquo;s Due Dates tab.
            </p>
          </div>
        )}
      </Modal>
      <Modal open={deleting} size="md" title="Delete this draft setup?"
        onClose={() => { if (!busy) setDeleting(false); }}
        footer={<div className="flex items-center justify-end gap-2">
          <Button variant="secondary" disabled={!!busy} onClick={() => setDeleting(false)}>Cancel</Button>
          <Button variant="danger" disabled={!!busy} onClick={async () => {
            setBusy("delete");
            try { await deleteSetup(cid); nav("/direct/setups?tab=reinsurance"); }
            catch (e) { setErr(fieldErrors(e).message); setDeleting(false); }
            finally { setBusy(""); }
          }}>Delete Draft</Button>
        </div>}>
        <p className="text-sm text-ink-muted">
          The treaty goes back to <b className="text-ink">not set up</b>: nothing is prepared for its
          reinsurers until a setup is made again. Statements already made are kept.
        </p>
      </Modal>
    </>
  );
}
