/**
 * "Create Output BDX Template" for a reinsurance bordereau — the broker
 * setup's dialog of that name (CreateOutputTemplate), for a treaty.
 *
 * The scope (treaty → programmes → input) is shown at the top and not
 * editable here: it was chosen on the setup screen behind the dialog.
 *
 * Two ways to build one, as the broker's:
 *
 *   A reporting standard — the Lloyd's risk sheet for a territory: its
 *   published columns, the mandatory ones and those the bordereaux can fill
 *   ticked, then the treaty's reinsurance columns.
 *
 *   The treaty and contracts — the treaty's type, terms and rules decide the
 *   columns, each with the term that put it there.
 *
 * Either way the programmes feeding the treaty have CONTRACTS as well as the
 * treaty, and "Read the terms from" lists both: the treaty (always read — the
 * statement is its) and each programme's contract, ticked by default. What a
 * ticked contract asks the bordereau to report is added as further columns,
 * read as a broker's output template reads its contract.
 *
 * Either way the proposal is reviewed — tick or untick each column — before
 * anything is added to the setup, and nothing is saved until the setup is.
 */
import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, FileSpreadsheet, FileText, Sparkles } from "lucide-react";
import { Modal } from "../ui/Modal";
import Button from "../ui/Button";
import { Field, Select, TextInput } from "../ui/Field";
import { ChoiceCard } from "../CreateOutputTemplate";
import { getStandards, type Standard } from "../../api/outputTemplate";
import { fieldErrors } from "../../api/contractRecord";
import {
  contractColumns, lloydsLayout, treatyLayout, type LayoutProposal, type SetupRecord,
  type SetupValues,
} from "../../api/ceded";

type Mode = "choose" | "standard" | "contract";
type Created = Pick<SetupValues, "layout" | "columns" | "layout_file" | "file_format">;

export default function CreateCededTemplate({ open, onClose, onCreated, cid, rec, v }: {
  open: boolean;
  onClose: () => void;
  /** The template, ready to go into the setup (saved with it). */
  onCreated: (t: Created) => void;
  cid: number; rec: SetupRecord; v: SetupValues;
}) {
  const [mode, setMode] = useState<Mode>("choose");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [standards, setStandards] = useState<Standard[]>([]);
  const [standardId, setStandardId] = useState("");
  const [jurisdiction, setJurisdiction] = useState("");
  const [format, setFormat] = useState<"xlsx" | "csv">("xlsx");
  const [sheetName, setSheetName] = useState("");
  const [proposal, setProposal] = useState<LayoutProposal | null>(null);
  // Columns unticked in the proposal, by position.
  const [dropped, setDropped] = useState<Set<number>>(new Set());
  // The programmes' contracts NOT read — all are read unless unticked.
  const [skipped, setSkipped] = useState<Set<number>>(new Set());
  const [clauses, setClauses] = useState(0);
  const [warn, setWarn] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setMode("choose"); setErr(null); setProposal(null); setDropped(new Set()); setSheetName("");
    setSkipped(new Set()); setClauses(0); setWarn(null);
    getStandards().then(d => {
      setStandards(d.standards);
      const first = d.standards[0];
      if (first) {
        setStandardId(prev => prev || first.id);
        setJurisdiction(prev => prev || first.default_jurisdiction || first.jurisdictions[0] || "");
      }
    }).catch(() => setStandards([]));
  }, [open]);

  const std = standards.find(s => s.id === standardId) ?? null;
  useEffect(() => {
    if (std && !std.jurisdictions.includes(jurisdiction))
      setJurisdiction(std.default_jurisdiction || std.jurisdictions[0] || "");
  }, [std, jurisdiction]);

  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const progs = rec.options.programmes.filter(p => v.program_ids == null || v.program_ids.includes(p.id));
  const contracts = (rec.options.contracts ?? [])
    .filter(k => k.program_id == null || progs.some(p => p.id === k.program_id));
  const reading = contracts.filter(k => !skipped.has(k.id));
  const scope: [string, string | null][] = [
    ["Treaty", rec.treaty],
    ["Treaty type", rec.type_label],
    ["Programmes", progs.map(p => p.name).join(", ") || null],
    ["Contracts", contracts.length ? `${contracts.length} on the programmes` : null],
    ["Input", own ? (own.filename ?? "Your input file") : "Your brokers' bordereaux"],
  ];

  async function propose(m: Mode) {
    setBusy(true); setErr(null);
    try {
      const p = m === "standard"
        ? await lloydsLayout(cid, jurisdiction, standardId || undefined)
        : await treatyLayout(cid);
      // Then what the ticked contracts ask for, after the sheet's own columns.
      setWarn(null); setClauses(0);
      if (reading.length) {
        try {
          const more = await contractColumns(cid, reading.map(k => k.id), p.columns.map(c => c.heading));
          p.columns = [...p.columns, ...more.columns];
          p.unfilled = [...(p.unfilled ?? []), ...more.unfilled];
          setClauses(more.clause_count);
        } catch (e) {
          setWarn(`The contracts could not be read (${fieldErrors(e).message}) — the columns below are the ${m === "standard" ? "standard's and the treaty's" : "treaty's"} only.`);
        }
      }
      setProposal(p);
      // What the proposal itself leaves out starts unticked.
      setDropped(new Set(p.columns.flatMap((c, i) => c.keep === false ? [i] : [])));
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  const locked = (i: number) => mode === "standard" && !!proposal?.columns[i]?.required;
  const keptIdx = useMemo(() => (proposal?.columns ?? []).map((_, i) => i)
    .filter(i => !dropped.has(i) || locked(i)), [proposal, dropped, mode]); // eslint-disable-line react-hooks/exhaustive-deps
  const kept = keptIdx.map(i => proposal!.columns[i]);
  const unfilled = (proposal?.unfilled ?? []).filter(h => kept.some(c => c.heading === h)).length;
  const fromTreaty = kept.filter(c => !c.by && (mode === "contract" || c.added)).length;
  const fromContracts = kept.filter(c => !!c.by).length;
  const leftOut = (proposal?.columns.length ?? 0) - kept.length;
  const stdLabel = proposal?.layout.standard
    ? `${proposal.layout.standard.label} (${proposal.layout.standard.jurisdiction})` : std?.label ?? "The standard";

  function toggle(i: number) {
    setDropped(prev => {
      const n = new Set(prev);
      if (n.has(i)) n.delete(i); else n.add(i);
      return n;
    });
  }

  function create() {
    if (!proposal) return;
    const keep = new Set(keptIdx);
    // A Lloyd's sheet keeps its published columns, unticked ones left out of
    // the file; a treaty-built sheet is just the columns ticked.
    const columns = mode === "standard"
      ? proposal.columns.map((c, i) => ({ ...c, keep: keep.has(i) }))
      : proposal.columns.filter((_, i) => keep.has(i));
    onCreated({
      layout: { ...proposal.layout, ...(sheetName.trim() ? { sheet_name: sheetName.trim() } : {}) },
      columns, layout_file: proposal.layout_file, file_format: format,
    });
  }

  const back = () => { setProposal(null); setDropped(new Set()); setErr(null); };

  return (
    <Modal open={open} size="3xl" onClose={() => { if (!busy) onClose(); }}
      title="Create Output BDX Template">
      <div className="space-y-4">
        <div className="rounded-lg border border-border bg-surface-2 px-3 py-2.5">
          <div className="flex flex-wrap gap-x-6 gap-y-1.5">
            {scope.map(([label, value]) => (
              <div key={label} className="min-w-0 max-w-[260px]">
                <div className="text-[10.5px] uppercase tracking-wide text-ink-soft">{label}</div>
                <div className={`truncate text-[12.5px] ${value ? "font-medium" : "text-ink-soft"}`}
                  title={value ?? undefined}>{value || "—"}</div>
              </div>
            ))}
          </div>
        </div>

        {err && (
          <div className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-[12px] text-red-700">
            <AlertTriangle size={15} className="mt-0.5 shrink-0" /><div>{err}</div>
          </div>
        )}
        {busy && <div className="text-sm text-ink-muted">
          Working out the columns{reading.length ? ` — reading ${reading.length} contract${reading.length === 1 ? "" : "s"}` : ""}…
        </div>}
        {warn && proposal && (
          <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-[12px] text-amber-800">
            <AlertTriangle size={15} className="mt-0.5 shrink-0" /><div>{warn}</div>
          </div>
        )}

        {mode === "choose" && (
          <div className="space-y-3">
            <p className="text-sm text-ink-muted">How should the column list be decided?</p>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <ChoiceCard icon={<FileSpreadsheet size={17} />}
                title={standards.length
                  ? `${standards.map(s => s.label).join(" / ")} format` : "Reporting standard format"}
                body={standards.length
                  ? ["Based on the Lloyd's report format",
                     "Your input bordereaux are checked to keep only the columns you can fill",
                     "Then the treaty's reinsurance columns — ceded premium, commission, each reinsurer's share"]
                  : "No reporting standard is bundled with this deployment."}
                disabled={!standards.length}
                onClick={() => setMode("standard")} />
              <ChoiceCard icon={<FileText size={17} />} title="Treaty and contracts"
                body={rec.has_structure
                  ? ["Based on your treaty and the programmes' contracts",
                     "A column for what the treaty works out, for what its terms quote, and for every field a treaty rule checks",
                     "Plus what each contract asks the bordereau to report"]
                  : "Set out the treaty's structure first."}
                disabled={!rec.has_structure}
                onClick={() => setMode("contract")} />
            </div>
          </div>
        )}

        {mode !== "choose" && !proposal && (
          <div className="space-y-3">
            <div className="rounded-lg border border-border bg-surface-2 px-3 py-2.5 text-[12px] leading-relaxed">
              <div className="flex items-center gap-1.5 font-medium text-ink">
                {mode === "standard" ? <FileSpreadsheet size={14} /> : <FileText size={14} />}
                {mode === "standard" ? "How the columns are chosen" : "Building from the treaty"}
              </div>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-ink-muted">
                {mode === "standard" ? (<>
                  <li><b>{std?.label ?? "The standard"}</b> gives the full list of allowed columns.</li>
                  <li><b>Your input bordereaux</b> show which ones you can actually fill.</li>
                  <li><b>Your treaty</b> adds the reinsurance columns the standard doesn&apos;t have.</li>
                  {reading.length > 0 && <li><b>The contracts you tick</b> add what they ask for that the standard doesn&apos;t have.</li>}
                </>) : (<>
                  <li>Columns come from what <b>{rec.treaty}</b> sets out — its type, terms and rules.</li>
                  {reading.length > 0 && <li>Plus what <b>the contracts you tick</b> ask the bordereau to report.</li>}
                  <li>Each column says which term or clause put it there.</li>
                </>)}
              </ul>
              <div className="mt-1 text-ink-muted">You&apos;ll review the result before anything is added.</div>
            </div>

            <div>
              <div className="mb-1 text-[12.5px] font-medium text-ink">Read the terms from</div>
              <div className="max-h-48 space-y-1 overflow-auto rounded-lg border border-border bg-white p-2">
                <label className="flex items-start gap-2 rounded px-1.5 py-1 text-[12.5px]">
                  <input type="checkbox" className="mt-0.5" checked disabled
                    title="Always read — the statement is this treaty's" />
                  <span className="min-w-0">
                    <b>{rec.treaty}</b>
                    <span className="ml-1.5 rounded-full bg-navy/10 px-1.5 py-0.5 text-[10px] font-semibold text-navy">Treaty</span>
                    <span className="block text-[11.5px] text-ink-muted">
                      Always read — {mode === "standard" ? "its reinsurance columns" : "its type, terms and rules"}
                    </span>
                  </span>
                </label>
                {contracts.map(k => (
                  <label key={k.id} className="flex cursor-pointer items-start gap-2 rounded px-1.5 py-1 text-[12.5px] hover:bg-surface-2">
                    <input type="checkbox" className="mt-0.5" disabled={busy} checked={!skipped.has(k.id)}
                      onChange={() => setSkipped(prev => {
                        const n = new Set(prev);
                        if (n.has(k.id)) n.delete(k.id); else n.add(k.id);
                        return n;
                      })} />
                    <span className="min-w-0">
                      <b>{k.name}</b>
                      <span className="ml-1.5 rounded-full bg-surface-2 px-1.5 py-0.5 text-[10px] font-semibold text-ink-muted">Contract</span>
                      <span className="block text-[11.5px] text-ink-muted">
                        {[k.programme, k.broker].filter(Boolean).join(" · ") || "On a programme this treaty protects"}
                      </span>
                    </span>
                  </label>
                ))}
                {contracts.length === 0 && (
                  <div className="px-1.5 py-1 text-[11.5px] text-ink-soft">
                    No contracts on the programmes this treaty protects.
                  </div>
                )}
              </div>
            </div>

            <div className={`grid grid-cols-1 items-end gap-3 ${mode === "standard" ? "sm:grid-cols-3" : "sm:grid-cols-2"}`}>
              {mode === "standard" && (<>
                <Field label="Standard & version">
                  <Select value={standardId} disabled={busy} onChange={e => setStandardId(e.target.value)}>
                    {standards.map(s => <option key={s.id} value={s.id}>{s.label}</option>)}
                  </Select>
                </Field>
                <Field label="Territory / jurisdiction">
                  <Select value={jurisdiction} disabled={busy || !std} onChange={e => setJurisdiction(e.target.value)}>
                    {(std?.jurisdictions ?? []).map(j => <option key={j} value={j}>{j}</option>)}
                  </Select>
                </Field>
              </>)}
              <Field label="Output format">
                <Select value={format} disabled={busy} onChange={e => setFormat(e.target.value as "xlsx" | "csv")}>
                  <option value="xlsx">XLSX</option>
                  <option value="csv">CSV</option>
                </Select>
              </Field>
              {mode === "contract" && (
                <Field label="Sheet name (optional)">
                  <TextInput value={sheetName} disabled={busy} placeholder="Lines"
                    onChange={e => setSheetName(e.target.value)} />
                </Field>
              )}
            </div>
            {mode === "standard" && (
              <Field label="Sheet name (optional)">
                <TextInput value={sheetName} disabled={busy} placeholder="Left blank, the standard's own name"
                  onChange={e => setSheetName(e.target.value)} />
              </Field>
            )}

            <div className="flex justify-end gap-2 pt-1">
              <Button variant="secondary" disabled={busy} onClick={() => setMode("choose")}>Back</Button>
              <Button onClick={() => propose(mode)} disabled={busy || (mode === "standard" && !std)}>
                <Sparkles size={14} /> Work out the columns
              </Button>
            </div>
          </div>
        )}

        {proposal && (
          <div className="space-y-3">
            <div className="rounded-lg border border-border bg-surface-2 px-3 py-2.5 text-[12px] leading-relaxed">
              <div className="flex items-center gap-1.5 font-medium text-ink">
                <CheckCircle2 size={14} className="text-emerald-600" />
                {kept.length} of {proposal.columns.length} column{proposal.columns.length === 1 ? "" : "s"} selected
                {mode === "standard" ? <> from {stdLabel}</> : <> from the treaty</>}
              </div>
              <ul className="mt-1 list-disc space-y-0.5 pl-4 text-ink-muted">
                <li>Checked against {own ? "your input file" : "your brokers' bordereau templates"}</li>
                {mode === "standard" && fromTreaty > 0 && (
                  <li>{fromTreaty} reinsurance column{fromTreaty === 1 ? " is" : "s are"} added at the end from the treaty</li>
                )}
                {fromContracts > 0 && (
                  <li>{fromContracts} column{fromContracts === 1 ? " comes" : "s come"} from the
                    contracts{clauses > 0 && ` (read from ${clauses} clause${clauses === 1 ? "" : "s"})`}</li>
                )}
                {!warn && reading.length > 0 && fromContracts === 0 && (
                  <li>The contracts ask for nothing the sheet doesn&apos;t already have</li>
                )}
                {unfilled > 0 && (
                  <li>{unfilled} ticked column{unfilled === 1 ? " has" : "s have"} no data in your input — left
                    blank until matched on Field Mapping</li>
                )}
                {leftOut > 0 && <li>{leftOut} column{leftOut === 1 ? "" : "s"} left out as not needed</li>}
              </ul>
            </div>

            <div className="max-h-80 overflow-auto rounded-lg border border-border">
              <table className="proposal-grid w-full table-fixed text-[12.5px]">
                <colgroup>
                  <col style={{ width: 44 }} /><col style={{ width: "30%" }} />
                  <col style={{ width: "16%" }} /><col />
                </colgroup>
                <thead>
                  <tr>
                    <th className="py-2 pl-3 text-left">Use</th>
                    <th className="px-3 py-2 text-left">Column</th>
                    <th className="px-3 py-2 text-left">Required by</th>
                    <th className="px-3 py-2 text-left">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {proposal.columns.map((c, i) => {
                    const on = !dropped.has(i) || locked(i);
                    const fromStd = mode === "standard" && !c.added;
                    const blank = (proposal.unfilled ?? []).includes(c.heading);
                    return (
                      <tr key={`${c.key}-${i}`} className={`border-t border-border ${on ? "" : "opacity-50"}`}>
                        <td className="py-2 pl-3 text-left align-top">
                          <input type="checkbox" checked={on} disabled={locked(i)}
                            title={locked(i) ? "The standard makes this column mandatory" : undefined}
                            onChange={() => toggle(i)} />
                        </td>
                        <td className="px-3 py-2 text-left align-top font-medium">
                          <span className="break-words">{c.heading}</span>
                          {fromStd && c.required && (
                            <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 py-0.5 align-middle text-[10px] text-amber-800">required</span>
                          )}
                          {mode === "standard" && c.added && (
                            <span className="ml-1.5 rounded-full bg-sky-100 px-1.5 py-0.5 align-middle text-[10px] text-sky-800">extra column</span>
                          )}
                        </td>
                        <td className="break-words px-3 py-2 text-left align-top text-ink-muted">
                          {c.by ?? (fromStd ? stdLabel : "The treaty")}
                        </td>
                        <td className="px-3 py-2 text-left align-top text-ink-muted">
                          {(c.by && c.why?.startsWith(`${c.by}: `) ? c.why.slice(c.by.length + 2) : c.why) ?? (fromStd
                            ? (c.required ? "Mandatory in this territory"
                              : blank ? "Published column — your input has no data for it"
                              : "Published column your input can fill")
                            : "A reinsurance figure the treaty works out")}
                          {c.clause && (
                            <div className="mt-0.5 line-clamp-2 text-[11px] italic text-ink-soft" title={c.clause}>
                              &ldquo;{c.clause}&rdquo;
                            </div>
                          )}
                          {blank && on && !fromStd && (
                            <div className="mt-0.5 text-[11px] text-amber-700">No data in your input yet</div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-ink-soft">
              <li>Nothing is saved yet — it is saved with the setup</li>
              <li>Ticked columns are added; unticked ones are left out</li>
              {mode === "standard" && proposal.columns.some(c => c.required) && (
                <li>Required columns are always added</li>
              )}
              <li>You can add, rename, reorder or remove columns later on the Output BDX Template step</li>
            </ul>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="secondary" disabled={busy} onClick={back}>Back</Button>
              <Button onClick={create} disabled={busy || !kept.length}>
                Create Template ({kept.length} column{kept.length === 1 ? "" : "s"})
              </Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
