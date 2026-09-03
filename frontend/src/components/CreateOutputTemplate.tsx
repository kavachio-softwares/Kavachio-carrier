/**
 * "Create Output BDX Template" — the dialog behind the button of that name.
 *
 * The scope it is creating for (carrier -> programme -> broker -> contract) is
 * shown at the top and never editable here: it was chosen on the screen that
 * opened this, and a template silently filed against a different scope than the
 * one on screen is exactly the mistake this exists to prevent.
 *
 * Two ways to build one, and they answer different situations:
 *
 *   A reporting standard — the market already publishes the column list (the
 *   Lloyd's Coverholder Reporting Standards, say). Pick the version and the
 *   territory you report on and it is generated from the published workbook.
 *
 *   The contract — no published standard applies, so the contract's own terms
 *   decide what the bordereau has to carry.
 *
 * EITHER WAY, BOTH SIDES OF THE JOB ARE READ. A territory's published list runs
 * to hundreds of columns and most of them do not apply to a given binder, so
 * what the standard and the contract DEMAND is checked against what the
 * incoming bordereau can actually FILL, and every column arrives with a tick, a
 * reason and the input column it was matched to. Nothing is created until a
 * person has looked at that list — this dialog produces a first draft, which is
 * then reviewed, validated and activated in the editor.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, CheckCircle2, FileSpreadsheet, FileText, Sparkles,
} from "lucide-react";
import { Modal } from "./ui/Modal";
import Button from "./ui/Button";
import { Field, Select, TextInput } from "./ui/Field";
import {
  analyzeSources, createFromContract, createFromStandard, getStandards,
  type OutputTemplate, type ProposedField, type ScopedContract,
  type SourceAnalysis, type Standard,
} from "../api/outputTemplate";
import { errText } from "../utils/directSetup";

/** Which contract the field list is read from — saved, or staged in the form. */
type ContractPick =
  | { kind: "saved"; id: number; label: string }
  | { kind: "staged"; index: number; label: string };

export type CreateScopeProps = {
  open: boolean;
  onClose: () => void;
  onCreated: (t: OutputTemplate) => void;
  mga: string;
  programId: number;
  carrierPartyId: number | null;
  brokerPartyId: number | null;
  contractId: number | null;
  scopeNames: {
    carrier: string | null; programme: string | null;
    broker: string | null; contract: string | null;
  };
  /** The sample bordereau staged on the setup screen — the INPUT side. */
  inputFile?: File | null;
  /** Only these sheets of it are mapped, so only these decide the columns. */
  inputSheets?: string[];
  /** Contracts staged on the setup screen and not uploaded yet. */
  contractFiles?: File[];
  /** Contracts already approved and on file for this scope. */
  boundContracts?: ScopedContract[];
};

type Mode = "choose" | "standard" | "contract";

export default function CreateOutputTemplate(p: CreateScopeProps) {
  const [mode, setMode] = useState<Mode>("choose");
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");
  const [err, setErr] = useState<string | null>(null);

  // Which standards this deployment actually ships. Fetched when the dialog
  // opens rather than once on mount, so adding a standard to the server is
  // picked up without a page reload.
  const [standards, setStandards] = useState<Standard[]>([]);
  const [formats, setFormats] = useState<string[]>(["xlsx"]);
  const [standardId, setStandardId] = useState("");
  const [jurisdiction, setJurisdiction] = useState("");
  const [outputFormat, setOutputFormat] = useState("xlsx");
  const [name, setName] = useState("");
  const [useLibrary, setUseLibrary] = useState(true);

  // The proposal, and what the user unticked in it.
  const [analysis, setAnalysis] = useState<SourceAnalysis | null>(null);
  const [dropped, setDropped] = useState<Set<string>>(new Set());

  const std = standards.find(s => s.id === standardId) ?? null;

  // Every contract the field list could be read from — what is already on file
  // first, then what is staged on the screen behind this dialog. A staged one
  // counts: it is the contract the user is holding, and refusing to look at it
  // until it has been uploaded is why this option used to sit greyed out.
  const contractOptions = useMemo<ContractPick[]>(() => {
    const saved: ContractPick[] = (p.boundContracts ?? []).map(c => ({
      kind: "saved", id: c.id,
      label: c.filename || `Contract ${c.id}`,
    }));
    const staged: ContractPick[] = (p.contractFiles ?? []).map((f, i) => ({
      kind: "staged", index: i, label: f.name,
    }));
    // A staged file with the same name as one already on file is the same
    // contract being re-uploaded; the saved one wins because its terms have
    // already been extracted.
    const seen = new Set(saved.map(c => c.label.toLowerCase()));
    return [...saved, ...staged.filter(c => !seen.has(c.label.toLowerCase()))];
  }, [p.boundContracts, p.contractFiles]);

  const [contractKey, setContractKey] = useState("");
  const contract = useMemo(
    () => contractOptions.find(keyOf2(contractKey)) ?? contractOptions[0] ?? null,
    [contractOptions, contractKey]);

  const load = useCallback(() => {
    getStandards()
      .then(d => {
        setStandards(d.standards);
        setFormats(d.output_formats.length ? d.output_formats : ["xlsx"]);
        const first = d.standards[0];
        if (first) {
          setStandardId(prev => prev || first.id);
          setJurisdiction(prev => prev || first.default_jurisdiction || first.jurisdictions[0] || "");
        }
      })
      .catch(() => { setStandards([]); });
  }, []);

  useEffect(() => {
    if (!p.open) return;
    setMode("choose"); setErr(null); setAnalysis(null);
    setDropped(new Set()); setStep(""); setContractKey("");
    load();
  }, [p.open, load]);

  // Keep the jurisdiction inside the chosen standard: switching standards must
  // not leave a territory selected that the new one does not publish.
  useEffect(() => {
    if (!std) return;
    if (!std.jurisdictions.includes(jurisdiction)) {
      setJurisdiction(std.default_jurisdiction || std.jurisdictions[0] || "");
    }
  }, [std, jurisdiction]);

  const stagedFile = contract?.kind === "staged"
    ? (p.contractFiles ?? [])[contract.index] ?? null : null;
  const savedId = contract?.kind === "saved" ? contract.id : null;

  const scope = {
    mga: p.mga,
    program_id: p.programId,
    carrier_party_id: p.carrierPartyId,
    broker_party_id: p.brokerPartyId,
    contract_id: savedId ?? p.contractId,
    output_format: outputFormat,
    name: name.trim() || null,
  };

  /** Read both sides and propose the column list. Creates nothing. */
  async function propose(forMode: Mode) {
    setBusy(true); setErr(null);
    setStep(p.inputFile
      ? "Reading your bordereau and the contract, and working out which columns apply…"
      : "Reading the contract and the published field list…");
    try {
      const a = await analyzeSources({
        mga: p.mga,
        program_id: p.programId,
        broker_party_id: p.brokerPartyId,
        contract_id: savedId ?? p.contractId,
        standard_id: forMode === "standard" || useLibrary ? standardId : null,
        jurisdiction: forMode === "standard" || useLibrary ? jurisdiction : null,
        include_standard_library: forMode === "standard" ? true : useLibrary,
        // The standard plays a different part in each mode. Building FROM it,
        // its published list is the layout. Building from a contract, the
        // contract is the layout and the standard only fills in what contracts
        // never name — so only its mandatory fields come through, or the two
        // options would produce the same file.
        standard_scope: forMode === "standard" ? "full" : "essential",
        read_contract: true,
        inputFile: p.inputFile ?? null,
        inputSheets: p.inputSheets ?? [],
        // In standard mode nobody picks a contract — the field list comes from
        // the published standard — so everything to hand is read: the contract
        // already on file AND one staged on the screen behind. They are
        // different documents, and a staged one is usually the newer of the
        // two, so ignoring it would propose against last year's terms.
        contractFile: forMode === "standard"
          ? ((p.contractFiles ?? [])[0] ?? stagedFile) : stagedFile,
      });
      setAnalysis(a);
      setDropped(new Set(a.fields.filter(f => !f.recommended).map(f => f.field)));
    } catch (e: unknown) { setErr(errText(e)); }
    finally { setBusy(false); setStep(""); }
  }

  const kept = useMemo(
    () => (analysis?.fields ?? []).filter(f => !dropped.has(f.field)),
    [analysis, dropped]);

  // Columns the contract asks for that the published list has no column for.
  // They are built too, appended after the standard's own columns and always
  // optional — the standard is what makes a column mandatory, so a column that
  // exists because THIS contract asks for it must not fail the standard's
  // check. Leaving them out would ship a file that quietly omits something the
  // contract requires, with no empty column for anyone to notice.
  const extra = useMemo(
    () => mode === "standard"
      ? (analysis?.fields ?? []).filter(f => f.origin !== "standard") : [],
    [analysis, mode]);
  // What to call the published list in words, so a sentence about it names the
  // thing on screen ("Lloyd's v5.2 (US)") rather than "this territory".
  const stdLabel = analysis?.standard
    ? `${analysis.standard.label}${analysis.standard.jurisdiction
        ? ` (${analysis.standard.jurisdiction})` : ""}`
    : "the reporting standard";
  // Every proposed column is now tickable in both modes — there is no longer a
  // second, unbuildable class of field shown below the list.
  const proposed = analysis?.fields ?? [];
  const keptExtra = extra.filter(f => !dropped.has(f.field)).length;
  // "Required with nothing to fill it" counts only columns that will actually
  // be required. An appended contract column goes in optional, so an empty one
  // is a gap to chase, not a file the standard will reject.
  const unfilledRequired = kept.filter(
    f => f.required && !isAppended(f, mode)
      && !f.in_input && !f.likely_in_input).length;

  async function create() {
    if (!kept.length) { setErr("Keep at least one column."); return; }
    setBusy(true); setErr(null);
    setStep("Building the template…");
    try {
      if (mode === "standard") {
        // The kept list carries the contract's own columns too. The server
        // slices the published layout to what is ticked, then appends those
        // after it as optional columns.
        p.onCreated(await createFromStandard(
          scope, standardId, jurisdiction, kept));
      } else {
        p.onCreated(await createFromContract(
          { ...scope, contract_name: stagedFile?.name ?? contract?.label ?? null },
          {
            standard_id: useLibrary ? standardId : null,
            jurisdiction: useLibrary ? jurisdiction : null,
            include_standard_library: useLibrary,
            standard_scope: "essential",
            fields: kept,
            fields_reviewed: true,
          }));
      }
    } catch (e: unknown) { setErr(errText(e)); }
    finally { setBusy(false); setStep(""); }
  }

  const toggle = (field: string) =>
    setDropped(prev => {
      const n = new Set(prev);
      if (n.has(field)) n.delete(field); else n.add(field);
      return n;
    });

  const back = () => { setAnalysis(null); setDropped(new Set()); setErr(null); };

  return (
    <Modal open={p.open} size="3xl"
      onClose={() => { if (!busy) p.onClose(); }}
      title="Create Output BDX Template">
      <div className="space-y-4">
        <ScopeStrip names={p.scopeNames} />

        {err && (
          <div className="rounded-md border border-red-200 bg-red-50 p-3 text-[12px]
            text-red-700 flex items-start gap-2">
            <AlertTriangle size={15} className="mt-0.5 shrink-0" />
            <div>{err}</div>
          </div>
        )}
        {busy && step && <div className="text-sm text-ink-muted">{step}</div>}

        {mode === "choose" && (
          <div className="space-y-3">
            <p className="text-sm text-ink-muted">
              How should the column list be decided?
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <ChoiceCard
                icon={<FileSpreadsheet size={17} />}
                title="Reporting standard"
                body={standards.length
                  ? `The whole published column list — ${standards.map(s => s.label).join(", ")} — for the territory you report on, pruned to the part your binder actually uses. The layout is the standard's.`
                  : "No reporting standard is bundled with this deployment."}
                disabled={!standards.length}
                onClick={() => setMode("standard")} />
              <ChoiceCard
                icon={<FileText size={17} />}
                title="Contract-based / custom"
                body={contractOptions.length
                  ? `Only what ${contractOptions.length === 1 ? "the contract" : "a contract"} itself requires, plus the columns every bordereau carries and contracts never name — the coverholder, the insured, the period, the currency. The layout is the contract's.`
                  : "Upload a contract on the setup screen first — this option builds the field list from the contract's terms."}
                disabled={!contractOptions.length}
                onClick={() => setMode("contract")} />
            </div>
            <SourcesNote inputFile={p.inputFile ?? null}
              contracts={contractOptions.length} />
          </div>
        )}

        {mode !== "choose" && !analysis && (
          <div className="space-y-3">
            {/* The two forms share their standard/territory/format row, so
                without this they read as the same screen — and they are not:
                one starts from a published list, the other from a contract. */}
            <ModeLede mode={mode} standard={std?.label ?? "the standard"}
              contract={contract?.label ?? null} useLibrary={useLibrary} />

            {mode === "contract" && (
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" className="mt-1" checked={useLibrary} disabled={busy}
                  onChange={e => setUseLibrary(e.target.checked)} />
                <span>
                  <b>Add the columns every bordereau needs.</b>
                  <span className="block text-[11px] text-ink-soft">
                    A contract rarely names the coverholder, the insured, the
                    reporting period or the currency, and a bordereau always
                    carries them. With this on, the {std?.label ?? "standard"}{" "}
                    columns marked mandatory are added to the contract's own —
                    those only, not the whole published list, which would bury
                    the contract's terms. Anything both name appears once.
                  </span>
                </span>
              </label>
            )}

            {mode === "contract" && contractOptions.length > 0 && (
              <Field label="Read the terms from">
                <Select value={keyFor(contract)} disabled={busy}
                  onChange={e => setContractKey(e.target.value)}>
                  {contractOptions.map(c => (
                    <option key={keyFor(c)} value={keyFor(c)}>
                      {c.label}{c.kind === "staged" ? " — staged on this screen" : ""}
                    </option>
                  ))}
                </Select>
              </Field>
            )}

            {/* items-end: the contract-mode territory label runs to two lines,
                and without it that select drops below the other two. */}
            {(mode === "standard" || useLibrary) && (
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 items-end">
                <Field label={mode === "standard"
                  ? "Standard & version" : "Borrow the essentials from"}>
                  <Select value={standardId} disabled={busy}
                    onChange={e => setStandardId(e.target.value)}>
                    {standards.map(s => (
                      <option key={s.id} value={s.id}>{s.label}</option>
                    ))}
                  </Select>
                </Field>
                {/* Still asked for in contract mode, and it matters: which
                    columns a standard calls mandatory differs by territory —
                    20 for the US, 14 for Singapore Risk — so the essentials
                    borrowed here are not the same set everywhere. */}
                <Field label={mode === "standard"
                  ? "Territory / jurisdiction"
                  : "Territory (decides which are mandatory)"}>
                  <Select value={jurisdiction} disabled={busy || !std}
                    onChange={e => setJurisdiction(e.target.value)}>
                    {(std?.jurisdictions ?? []).map(j => (
                      <option key={j} value={j}>{j}</option>
                    ))}
                  </Select>
                </Field>
                <Field label="Output format">
                  <Select value={outputFormat} disabled={busy}
                    onChange={e => setOutputFormat(e.target.value)}>
                    {formats.map(f => (
                      <option key={f} value={f}>{f.toUpperCase()}</option>
                    ))}
                  </Select>
                </Field>
              </div>
            )}
            {mode === "contract" && !useLibrary && (
              <Field label="Output format">
                <Select value={outputFormat} disabled={busy}
                  onChange={e => setOutputFormat(e.target.value)}>
                  {formats.map(f => <option key={f} value={f}>{f.toUpperCase()}</option>)}
                </Select>
              </Field>
            )}

            <Field label="Template name (optional)">
              <TextInput value={name} disabled={busy}
                placeholder="Left blank, it is named after the scope above"
                onChange={e => setName(e.target.value)} />
            </Field>

            <SourcesNote inputFile={p.inputFile ?? null}
              contracts={contractOptions.length} />

            <div className="flex justify-end gap-2 pt-1">
              <Button variant="secondary" disabled={busy}
                onClick={() => setMode("choose")}>Back</Button>
              <Button onClick={() => propose(mode)}
                disabled={busy || (mode === "standard" && !std)}>
                <Sparkles size={14} /> Work out the columns
              </Button>
            </div>
          </div>
        )}

        {analysis && (
          <div className="space-y-3">
            <Summary a={analysis} shown={proposed} kept={kept}
              unfilled={unfilledRequired} extra={keptExtra} />

            {!analysis.contract.model_used && analysis.contract.source !== "none" && (
              <div className="rounded-md border border-amber-300 bg-amber-50 p-3
                text-[12px] text-amber-800 flex items-start gap-2">
                <AlertTriangle size={15} className="mt-0.5 shrink-0" />
                <div>
                  The contract's wording could not be read just now, so only the
                  fields its existing rules already name were added. You can add
                  the rest in the editor.
                </div>
              </div>
            )}

            {/* `proposal-grid` makes the header opaque and pins it. Without
                it the app-wide `thead th` background is 60% transparent, so
                rows scrolling underneath a sticky header show straight through
                it and the two sets of words sit on top of each other. */}
            <div className="max-h-80 overflow-auto rounded-lg border border-border">
              <table className="w-full text-[12.5px] proposal-grid">
                <colgroup>
                  <col style={{ width: 40 }} />
                  <col style={{ width: "27%" }} />
                  <col style={{ width: "13%" }} />
                  <col style={{ width: "20%" }} />
                  <col />
                </colgroup>
                <thead>
                  <tr>
                    <th className="text-left px-3 py-2">Use</th>
                    <th className="text-left px-3 py-2">Column</th>
                    <th className="text-left px-3 py-2">Asked for by</th>
                    <th className="text-left px-3 py-2">In your bordereau</th>
                    <th className="text-left px-3 py-2">Why</th>
                  </tr>
                </thead>
                <tbody>
                  {proposed.map(f => (
                    <ProposalRow key={f.field} f={f}
                      appended={isAppended(f, mode)}
                      kept={!dropped.has(f.field)}
                      onToggle={() => toggle(f.field)} />
                  ))}
                </tbody>
              </table>
            </div>

            {extra.length > 0 && (
              <div className="rounded-md border border-border bg-surface-2 p-3 text-[11.5px]">
                <div className="font-medium text-ink">
                  {keptExtra} extra column{keptExtra === 1 ? "" : "s"} the
                  contract asks for that {stdLabel} does not publish
                </div>
                <div className="text-ink-muted mt-1">
                  {extra.map(f => f.field).join(", ")}
                </div>
                <div className="text-ink-soft mt-1">
                  These are added after the published columns and go in as
                  optional, so the file still passes the standard's own check.
                  They are ticked in the list above like any other column —
                  untick one to leave it out.
                </div>
              </div>
            )}

            <p className="text-[11px] text-ink-soft">
              Nothing has been created yet. Ticked columns go in; unticked ones
              stay out. A column the standard marks mandatory goes in whatever
              you tick, and you can add or remove columns in the editor
              afterwards too.
            </p>
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="secondary" disabled={busy} onClick={back}>Back</Button>
              <Button onClick={create} disabled={busy || !kept.length}>
                Create Template ({kept.length} column
                {kept.length === 1 ? "" : "s"})
              </Button>
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

/** Which of the two you are building, said in the terms of what comes out.
 *
 * The two forms share a standard/territory/format row, so on the controls
 * alone they look like one screen reached twice. What differs is where the
 * column list COMES FROM, and that is what this says.
 */
function ModeLede({ mode, standard, contract, useLibrary }: {
  mode: Mode; standard: string; contract: string | null; useLibrary: boolean;
}) {
  const std = mode === "standard";
  return (
    <div className={`rounded-lg border px-3 py-2.5 text-[12px] leading-relaxed
      ${std ? "border-border bg-surface-2" : "border-border bg-surface-2"}`}>
      <div className="flex items-center gap-1.5 font-medium text-ink">
        {std ? <FileSpreadsheet size={14} /> : <FileText size={14} />}
        {std ? "Building from a reporting standard"
             : "Building from the contract"}
      </div>
      <div className="text-ink-muted mt-1">
        {std ? (
          <>The columns are {standard}&apos;s own published list for the
            territory you pick below, in its published order. The contract and
            your bordereau are read too, but only to decide which of those
            columns this binder actually reports — and to add anything the
            contract asks for that the list has no column for.</>
        ) : (
          <>The columns come from {contract ? <b>{contract}</b> : "the contract"}
            &apos;s own terms{useLibrary
              ? <>, plus the handful of columns every bordereau carries and
                  contracts never name — the coverholder, the insured, the
                  period, the currency.</>
              : <> and nothing else. Nothing is added for you.</>}
            {" "}The published list is not the layout here.</>
        )}
      </div>
    </div>
  );
}

/** Is this column being APPENDED to a published layout?
 *
 * Only in standard mode, and only for a column the standard does not publish.
 * Such a column is added after the published ones and forced optional, because
 * "required" in a standard-built template means the standard refuses the file
 * without it — which it cannot, for a column it never defined.
 *
 * In contract mode there is no published layout to append to: the contract IS
 * the field list, and a field it requires is required.
 */
function isAppended(f: ProposedField, mode: Mode): boolean {
  return mode === "standard" && f.origin !== "standard";
}

/** What this proposal was read from — said before it is read, and after. */
function SourcesNote({ inputFile, contracts }: {
  inputFile: File | null; contracts: number;
}) {
  return (
    <div className="rounded-md border border-sky-200 bg-sky-50 p-2.5
      text-[11.5px] text-sky-800 leading-relaxed">
      <div className="font-medium">Read from both sides</div>
      <div className="mt-0.5">
        {inputFile
          ? <>Your bordereau <b>{inputFile.name}</b> decides which columns can
              actually be filled, </>
          : <>No input template is staged, so nothing can be checked against your
              data and only the published requirements will decide, </>}
        {contracts
          ? <>and the contract decides what has to be reported.</>
          : <>and no contract is available to say what has to be reported.</>}
      </div>
    </div>
  );
}

function Summary({ a, shown, kept, unfilled, extra }: {
  a: SourceAnalysis; shown: ProposedField[]; kept: ProposedField[];
  unfilled: number; extra: number;
}) {
  const matched = kept.filter(f => f.in_input).length;
  const likely = kept.filter(f => f.likely_in_input).length;
  const fromContract = kept.filter(f => f.origin !== "standard").length;
  return (
    <div className="rounded-lg border border-border bg-surface-2 px-3 py-2.5
      text-[12px] leading-relaxed">
      <div className="flex items-center gap-1.5 font-medium text-ink">
        <CheckCircle2 size={14} className="text-emerald-600" />
        {kept.length} of {shown.length} column{shown.length === 1 ? "" : "s"} kept
        {/* Only say "from the standard" when the standard IS the layout.
            Built from a contract it supplies a handful of columns, and
            crediting the whole list to it would misread the file. */}
        {a.standard && a.standard.scope !== "essential" && (
          <> from {a.standard.label}
            {a.standard.jurisdiction ? ` · ${a.standard.jurisdiction}` : ""}</>
        )}
        {a.standard && a.standard.scope === "essential" && <> from the contract</>}
      </div>
      <div className="text-ink-muted mt-1">
        {a.counts.checked_input
          ? <>{matched} of them matched to a column of your bordereau
              {likely > 0 && <>, {likely} probable</>}</>
          : <>your bordereau was not checked</>}
        {fromContract > 0 && <> · {fromContract} asked for by the contract</>}
        {a.standard && a.standard.scope === "essential" && (
          <> · {a.standard.field_count} added as the columns every bordereau
            carries</>
        )}
        {a.contract.clause_count > 0 &&
          <> · the contract was read from {a.contract.clause_count} clause
            {a.contract.clause_count === 1 ? "" : "s"}</>}
        {" · "}{shown.length - kept.length} left out as not applicable
      </div>
      {extra > 0 && (
        <div className="text-ink-muted mt-1">
          {extra} of them {extra === 1 ? "is" : "are"} not in the published
          list — the contract asks for {extra === 1 ? "it" : "them"}, so{" "}
          {extra === 1 ? "it is" : "they are"} added at the end as optional
          column{extra === 1 ? "" : "s"}.
        </div>
      )}
      {unfilled > 0 && (
        // A pointer, not the report. The columns themselves, why each one
        // stayed in and what to do about it need room and the field editor
        // beside them, so they are on the template screen — naming the number
        // here is what sends someone there.
        <div className="text-ink-muted mt-1">
          {unfilled} of them {unfilled === 1 ? "is" : "are"} required with
          nothing in your bordereau to fill {unfilled === 1 ? "it" : "them"} —
          the template screen lists {unfilled === 1 ? "it" : "them"} once this
          is created.
        </div>
      )}
    </div>
  );
}

function ProposalRow({ f, kept, appended, onToggle }: {
  f: ProposedField; kept: boolean; appended: boolean; onToggle: () => void;
}) {
  // Locked means the server will keep it whatever the tick says: a column the
  // standard marks mandatory is never switched off (apply_selection).
  const locked = f.required && f.origin === "standard";
  return (
    <tr className={`border-t border-border ${kept ? "" : "opacity-50"}`}>
      <td className="px-3 py-2 text-left align-top">
        <input type="checkbox" checked={kept || locked} disabled={locked}
          title={locked ? "The standard makes this column mandatory" : undefined}
          onChange={onToggle} />
      </td>
      <td className="px-3 py-2 font-medium text-left align-top">
        <span className="break-words">{f.field}</span>
        {/* Only the standard can make a column mandatory. A column appended
            because the contract asks for it is built optional, so labelling it
            "required" here would promise something the template will not say. */}
        {f.required && !appended && (
          <span className="ml-1.5 text-[10px] rounded-full px-1.5 py-0.5
            bg-amber-100 text-amber-800 align-middle">required</span>
        )}
        {appended && (
          <span className="ml-1.5 text-[10px] rounded-full px-1.5 py-0.5
            bg-sky-100 text-sky-800 align-middle">extra column</span>
        )}
      </td>
      <td className="px-3 py-2 text-ink-muted text-left align-top break-words">
        {f.origin === "standard"
          ? (f.also_in_contract ? "The standard and the contract" : "The standard")
          : f.origin === "contract_rule" ? "A rule on the contract"
          : "The contract"}
      </td>
      <td className="px-3 py-2 text-left align-top">
        {f.in_input ? (
          <span className="text-emerald-700">
            {f.input_column}
            <span className="text-[10.5px] text-ink-soft ml-1">
              {Math.round(f.confidence * 100)}%
            </span>
          </span>
        ) : f.best_candidate ? (
          <span className="text-amber-700">
            {f.best_candidate.source}
            <span className="text-[10.5px] text-ink-soft ml-1">
              {Math.round(f.best_candidate.confidence * 100)}% — confirm
            </span>
          </span>
        ) : <span className="text-ink-soft">—</span>}
      </td>
      <td className="px-3 py-2 text-ink-muted text-left align-top">
        {f.recommend_reason}
        {f.contract_reference && (
          <div className="text-[11px] text-ink-soft italic mt-0.5">
            “{f.contract_reference}”
          </div>
        )}
      </td>
    </tr>
  );
}

function ChoiceCard({ icon, title, body, disabled, onClick }: {
  icon: React.ReactNode; title: string; body: string;
  disabled?: boolean; onClick: () => void;
}) {
  return (
    <button type="button" disabled={disabled} onClick={onClick}
      className={`text-left rounded-lg border p-4 transition
        ${disabled
          ? "border-border bg-surface-2 opacity-60 cursor-not-allowed"
          : "border-border hover:border-brand hover:bg-surface-2"}`}>
      <div className="flex items-center gap-2 font-medium text-sm">
        {icon} {title}
      </div>
      <p className="text-[12px] text-ink-muted mt-1.5 leading-relaxed">{body}</p>
    </button>
  );
}

/** The four levels this template will belong to, spelled out. */
export function ScopeStrip({ names }: {
  names: { carrier: string | null; programme: string | null;
           broker: string | null; contract: string | null };
}) {
  const parts: [string, string | null][] = [
    ["Carrier", names.carrier], ["Programme", names.programme],
    ["Broker", names.broker], ["Contract", names.contract],
  ];
  return (
    <div className="rounded-lg border border-border bg-surface-2 px-3 py-2.5">
      <div className="flex flex-wrap gap-x-6 gap-y-1.5">
        {parts.map(([label, value]) => (
          <div key={label} className="min-w-0">
            <div className="text-[10.5px] uppercase tracking-wide text-ink-soft">{label}</div>
            <div className={`text-[12.5px] truncate ${value ? "font-medium" : "text-ink-soft"}`}>
              {value || "—"}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// A saved contract and a staged file need one addressable key between them.
const keyFor = (c: ContractPick | null) =>
  !c ? "" : c.kind === "saved" ? `saved:${c.id}` : `staged:${c.index}`;
const keyOf2 = (key: string) => (c: ContractPick) => keyFor(c) === key;
