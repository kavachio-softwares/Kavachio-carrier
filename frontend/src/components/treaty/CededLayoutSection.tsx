/**
 * Section 3 of a reinsurance bordereau setup — the file each reinsurer gets.
 *
 * Three ways to lay it out, like a broker's output template (Kavachio's own
 * standard layout is no longer offered; a setup saved with it keeps it until
 * another is chosen):
 *
 *   Your own file       the carrier already sends an Excel or CSV — upload it
 *                       once and every statement is that file, filled in
 *   Lloyd's risk sheet  no file of its own: the territory's published columns,
 *                       then the treaty's reinsurance columns
 *   From the treaty     built from the treaty itself: what its type works out,
 *                       what its terms quote, every field a treaty rule checks —
 *                       each column saying which term put it there
 *
 * For the last two every column says what fills it, read from its heading
 * (the same AI pass a broker's sample gets) and changeable here.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, ArrowDown, ArrowUp, FileSpreadsheet, Loader2, Plus, Search, Sparkles, Upload, X,
} from "lucide-react";
import { InfoTip } from "../InfoTip";
import { fieldErrors } from "../../api/contractRecord";
import {
  lloydsLayout, treatyLayout, uploadLayout, type LayoutProposal, type LayoutSource,
  type SetupColumn, type SetupRecord, type SetupValues,
} from "../../api/ceded";
import { getStandards, type Standard } from "../../api/outputTemplate";

type Props = {
  cid: number;
  rec: SetupRecord;
  v: SetupValues;
  change: (patch: Partial<SetupValues>) => void;
  bad: (k: string) => React.ReactNode;
};

// The broker setup's two ways to get an output template — upload one, or
// create one from the Lloyd's standard — plus one built from the treaty.
const SOURCES: { key: LayoutSource; label: string }[] = [
  { key: "contract", label: "Create from the treaty's terms" },
  { key: "uploaded", label: "Upload output template" },
  { key: "standard", label: "Create from Lloyd's risk sheet" },
];

/** A layout as it was last seen for each source, so switching back and forth
 *  does not lose an upload. */
type Held = Pick<SetupValues, "columns" | "layout" | "layout_file" | "file_format">;

const LBL: React.CSSProperties = {
  display: "flex", gap: 8, alignItems: "center", justifyContent: "flex-start",
  textAlign: "left", whiteSpace: "nowrap",
};

export default function CededLayoutSection({ cid, rec, v, change, bad }: Props) {
  const source: LayoutSource = v.layout?.source ?? "kavachio";
  const held = useRef<Partial<Record<LayoutSource, Held>>>({});
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [unfilled, setUnfilled] = useState<string[]>([]);
  const [standards, setStandards] = useState<Standard[]>([]);
  const [jurisdiction, setJurisdiction] = useState("");
  const [q, setQ] = useState("");
  const [onlyTicked, setOnlyTicked] = useState(true);
  // A column the carrier adds to a built sheet.
  const [adding, setAdding] = useState(false);
  const [newHeading, setNewHeading] = useState("");
  const [newKey, setNewKey] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  // What is saved is held for its own source from the start.
  useEffect(() => {
    const s = rec.setup;
    if (s?.layout && s.layout.source !== "kavachio") {
      held.current[s.layout.source] = {
        columns: s.columns, layout: s.layout, layout_file: null, file_format: s.file_format,
      };
    }
  }, [rec]);

  useEffect(() => {
    if (source !== "standard" || standards.length) return;
    getStandards().then(d => {
      setStandards(d.standards);
      const first = d.standards[0];
      if (first) setJurisdiction(j => j || v.layout?.standard?.jurisdiction
        || first.default_jurisdiction || first.jurisdictions[0] || "");
    }).catch(() => setStandards([]));
  }, [source, standards.length, v.layout?.standard?.jurisdiction]);

  function pick(next: LayoutSource) {
    if (next === source) return;
    held.current[source] = {
      columns: v.columns, layout: v.layout, layout_file: v.layout_file, file_format: v.file_format,
    };
    setErr(""); setUnfilled([]);
    const back = held.current[next];
    if (back) { change(back); return; }
    if (next === "contract") {
      // Built straight away — there is nothing to choose first.
      void read("treaty", () => treatyLayout(cid));
    } else {
      change({ layout: { source: next }, columns: [], layout_file: null });
    }
  }

  async function read(label: string, fn: () => Promise<LayoutProposal>) {
    setBusy(label); setErr("");
    try {
      const p = await fn();
      // Building again from the treaty keeps the columns the carrier added.
      const mine = label === "treaty"
        ? v.columns.filter(c => c.why === "Added by you"
            && !p.columns.some(x => x.heading === c.heading)) : [];
      change({ layout: p.layout, columns: [...p.columns, ...mine], layout_file: p.layout_file,
               file_format: p.file_format });
      setUnfilled(p.unfilled);
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy("");
    }
  }

  // From the carrier's own file when the setup reads one — as it stands on
  // screen, before it is saved.
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const fieldOpts = useMemo(() => own
    ? [...new Set(own.columns.map(c => c.field).filter(Boolean) as string[])]
        .map(f => ({ key: `field:${f}`,
                     label: rec.options.all_fields.find(x => x.key === f)?.label ?? f }))
    : rec.options.fields, [own, rec.options.fields, rec.options.all_fields]);
  const figureOpts = useMemo(() => [
    ...rec.options.columns.map(c => ({ key: c.key, label: c.label })),
    rec.options.share,
  ], [rec.options.columns, rec.options.share]);
  const known = useMemo(() => new Set([...figureOpts, ...fieldOpts, ...rec.options.context]
    .map(o => o.key)), [figureOpts, fieldOpts, rec.options.context]);
  const hasData = (key: string) => !!key && (!key.startsWith("field:")
    || fieldOpts.some(f => f.key === key));

  const setCol = (i: number, patch: Partial<SetupColumn>) =>
    change({ columns: v.columns.map((c, j) => j === i ? { ...c, ...patch } : c) });

  function moveCol(i: number, d: -1 | 1) {
    const cols = [...v.columns];
    const j = i + d;
    if (j < 0 || j >= cols.length) return;
    [cols[i], cols[j]] = [cols[j], cols[i]];
    change({ columns: cols });
  }

  const hasLayout = source !== "kavachio" && v.columns.length > 0;
  // A sheet Kavachio writes fresh (built from the treaty or the Lloyd's sheet)
  // is the carrier's to change: add, rename, reorder and remove columns. An
  // uploaded file is filled in exactly as it is — its columns are the file's.
  const editable = source === "contract" || source === "standard";
  const MAP = "__map";
  // What can fill a column chosen here; an input column is chosen on Field Mapping.
  const fillOpts = [
    { group: "Worked out for the treaty", opts: figureOpts },
    { group: "The same on every line", opts: rec.options.context },
  ];
  const fillValue = (key: string) =>
    fillOpts.some(g => g.opts.some(o => o.key === key)) ? key : MAP;
  function addColumn() {
    const heading = newHeading.trim();
    if (!heading) return;
    change({ columns: [...v.columns, {
      key: newKey === MAP ? "" : newKey, heading, column_index: null, keep: true,
      required: false, ref: null, why: "Added by you" }] });
    setNewHeading(""); setNewKey(""); setAdding(false);
  }
  const kept = v.columns.filter(c => c.keep !== false);
  const blank = kept.filter(c => !hasData(c.key)).length;

  const shown = v.columns
    .map((c, i) => ({ c, i }))
    .filter(({ c }) => source !== "standard" || !onlyTicked || c.keep !== false)
    .filter(({ c }) => !q.trim() || c.heading.toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <div>
      <div className="fh" style={{ marginTop: 0 }}>
        Output columns
        <InfoTip text="Build it from the treaty's own terms, upload the Excel or CSV you already send, or create one from the Lloyd's risk sheet — the same choices as a broker's output template. Every Excel file also has a summary sheet with the reinsurer's share and totals." />
      </div>
      <div className="seg" style={{ marginBottom: 12, flexWrap: "wrap" }}>
        {SOURCES.map(s => (
          <button key={s.key} type="button" className={source === s.key ? "on" : ""}
                  onClick={() => pick(s.key)}>{s.label}</button>
        ))}
      </div>
      {bad("layout")}

      {/* ── Kavachio's own — no longer offered; a setup saved with it keeps it ── */}
      {source === "kavachio" && (
        <>
          <div className="note warn" style={{ marginBottom: 12 }}>
            <b>This setup uses Kavachio&rsquo;s standard layout, which is no longer offered.</b>{" "}
            It keeps working until you choose another above —{" "}
            <b>Create from the treaty&rsquo;s terms</b> builds the sheet from this treaty.
          </div>
          <div className="seg sm" style={{ display: "flex", width: "fit-content", marginBottom: 12 }}>
            {(["xlsx", "csv"] as const).map(f => (
              <button key={f} type="button" className={v.file_format === f ? "on" : ""}
                      onClick={() => change({ file_format: f })}>
                {f === "xlsx" ? "Excel" : "CSV"}
              </button>
            ))}
          </div>
          <div className="tbl-wrap">
            <table className="panel-tbl">
              <thead><tr><th style={{ width: "38%" }}>Column</th><th>Heading in the file</th><th style={{ width: 90 }} /></tr></thead>
              <tbody>
                {v.columns.map((c, i) => (
                  <tr key={c.key}>
                    <td>
                      <label style={LBL}>
                        <input type="checkbox" checked
                               onChange={() => change({ columns: v.columns.filter(x => x.key !== c.key) })} />
                        {figureOpts.find(o => o.key === c.key)?.label ?? c.key}
                      </label>
                    </td>
                    <td><input value={c.heading} onChange={e => setCol(i, { heading: e.target.value })} /></td>
                    <td>
                      <button className="btn sm ghost" type="button" aria-label="Move up"
                              disabled={i === 0} onClick={() => moveCol(i, -1)}><ArrowUp size={12} /></button>
                      <button className="btn sm ghost" type="button" aria-label="Move down"
                              disabled={i === v.columns.length - 1} onClick={() => moveCol(i, 1)}><ArrowDown size={12} /></button>
                    </td>
                  </tr>
                ))}
                {rec.options.columns.filter(c => !v.columns.some(x => x.key === c.key)).map(c => (
                  <tr key={c.key} style={{ opacity: .6 }}>
                    <td>
                      <label style={LBL}>
                        <input type="checkbox" checked={false}
                               onChange={() => change({ columns: [...v.columns, { key: c.key, heading: c.label }] })} />
                        {c.label}
                      </label>
                    </td>
                    <td className="sub">not in the file</td><td />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {/* ── the carrier's own file ── */}
      {source === "uploaded" && (
        <div style={{ marginBottom: hasLayout ? 12 : 0 }}>
          <input ref={fileRef} type="file" accept=".xlsx,.xlsm,.csv" hidden
                 onChange={e => {
                   const f = e.target.files?.[0];
                   e.target.value = "";
                   if (f) read("upload", () => uploadLayout(cid, f));
                 }} />
          {hasLayout ? (
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <FileSpreadsheet size={18} className="ci" />
              <div style={{ flex: 1, minWidth: 0 }}>
                <b>{v.layout?.filename ?? "Your file"}</b>
                <div className="sub">
                  {v.file_format === "csv" ? "CSV" : `Excel · sheet “${v.layout?.sheet_name}”`}
                  {" · "}{v.columns.length} columns, filled in as they are — headings,
                  order and styling unchanged
                </div>
              </div>
              <button className="btn sm" type="button" disabled={!!busy}
                      onClick={() => fileRef.current?.click()}>
                <Upload size={12} /> Replace file
              </button>
            </div>
          ) : (
            <button type="button" className="dropzone" disabled={!!busy}
                    onClick={() => fileRef.current?.click()}
                    style={{ width: "100%", padding: "22px 16px", border: "1.5px dashed var(--p-border-2)",
                             borderRadius: 10, background: "var(--p-surface-2, transparent)",
                             cursor: "pointer", textAlign: "center" }}>
              <Upload size={18} />
              <div style={{ fontWeight: 600, marginTop: 6 }}>
                Upload the Excel or CSV you send your reinsurers
              </div>
              <div className="sub">
                A heading row, and sample rows if you like — they are cleared. Each
                column is matched to what fills it, and you can change any match.
              </div>
            </button>
          )}
        </div>
      )}

      {/* ── the Lloyd's risk sheet ── */}
      {source === "standard" && (
        <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap",
                      marginBottom: hasLayout ? 12 : 0 }}>
          {standards.length === 0 ? (
            <div className="sub">The Lloyd&rsquo;s standard is not available on this service.</div>
          ) : (
            <>
              <div className="field" style={{ marginBottom: 0, flex: "0 1 200px" }}>
                <label>Territory</label>
                <select value={jurisdiction} onChange={e => setJurisdiction(e.target.value)}>
                  {standards[0].jurisdictions.map(j => <option key={j} value={j}>{j}</option>)}
                </select>
              </div>
              <button className={`btn ${hasLayout ? "" : "pri"}`} type="button"
                      disabled={!!busy || !jurisdiction}
                      onClick={() => read("lloyds", () => lloydsLayout(cid, jurisdiction, standards[0].id))}>
                <Sparkles size={14} /> {hasLayout ? "Build again" : "Build from the Lloyd's risk sheet"}
              </button>
              {hasLayout && v.layout?.standard && (
                <span className="sub" style={{ flexBasis: "100%" }}>
                  {v.layout.standard.label} · {v.layout.standard.jurisdiction} ·{" "}
                  {kept.length} of {v.columns.length} columns in the file
                </span>
              )}
              {!hasLayout && (
                <span className="sub" style={{ flexBasis: "100%" }}>
                  The territory&rsquo;s published columns: the mandatory ones and those
                  your brokers&rsquo; bordereaux can fill are ticked, then the
                  treaty&rsquo;s reinsurance columns — ceded premium, commission, your share.
                </span>
              )}
            </>
          )}
        </div>
      )}

      {/* ── built from the treaty ── */}
      {source === "contract" && (
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap",
                      marginBottom: hasLayout ? 12 : 0 }}>
          <button className={`btn ${hasLayout ? "" : "pri"}`} type="button" disabled={!!busy}
                  onClick={() => read("treaty", () => treatyLayout(cid))}>
            <Sparkles size={14} /> {hasLayout ? "Build again from the treaty" : "Build from the treaty's terms"}
          </button>
          <span className="sub" style={{ flex: "1 1 320px" }}>
            {hasLayout
              ? `${kept.length} of ${v.columns.length} columns in the file — each says which term of the treaty put it there.`
              : "One column for what the treaty's type works out (cession, commission, recoveries…), "
                + "for what its terms quote, and for every field a treaty rule checks — "
                + "from its terms, its wording and its agreed limits."}
          </span>
        </div>
      )}

      {busy && (
        <div className="note" style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
          <Loader2 size={14} className="animate-spin" />
          {busy === "upload" ? "Reading your columns…"
            : busy === "treaty" ? "Reading the treaty's terms and rules…"
            : "Reading the Lloyd's columns — this can take a minute the first time…"}
        </div>
      )}
      {err && <div className="note warn" style={{ marginBottom: 12 }}>{err}</div>}

      {/* ── what fills each column ── */}
      {hasLayout && !busy && (
        <>
          <div className="sub" style={{ marginBottom: 8 }}>
            {editable
              ? <>Add, rename, reorder or remove any column — it is your sheet. Which of
                  your input columns fills one is set on the <b>Field Mapping</b> step.</>
              : <>These are your file&rsquo;s own columns, filled in as they are — to add
                  one, add it to the file and upload it again. Which of your input columns
                  fills each is set on the <b>Field Mapping</b> step.</>}
          </div>
          {source === "standard" && (
            <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
              <div style={{ position: "relative", flex: "1 1 200px" }}>
                <Search size={13} style={{ position: "absolute", left: 9, top: 10, opacity: .5 }} />
                <input value={q} onChange={e => setQ(e.target.value)} placeholder="Find a column…"
                       style={{ paddingLeft: 28 }} />
              </div>
              <label className="sub" style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input type="checkbox" checked={onlyTicked} style={{ width: "auto" }}
                       onChange={e => setOnlyTicked(e.target.checked)} />
                Only columns in the file
              </label>
            </div>
          )}
          <div className="tbl-wrap" style={{ maxHeight: 520, overflow: "auto" }}>
            <table className="panel-tbl">
              <thead>
                <tr><th>Column in the reinsurer&rsquo;s file</th></tr>
              </thead>
              <tbody>
                {shown.map(({ c, i }) => (
                  <tr key={i} style={c.keep === false ? { opacity: .6 } : undefined}>
                    <td>
                      <label style={{ ...LBL, whiteSpace: "normal" }}>
                        {(source === "standard" || source === "contract") && (
                          <input type="checkbox" checked={c.keep !== false}
                                 disabled={c.required && c.keep !== false}
                                 title={c.required ? "Mandatory in the Lloyd's standard" : undefined}
                                 onChange={e => setCol(i, { keep: e.target.checked })} />
                        )}
                        {editable && !c.required
                          ? <input value={c.heading} onChange={e => setCol(i, { heading: e.target.value })} />
                          : <span>{c.heading}</span>}
                        {editable && (
                          <span style={{ display: "flex", gap: 2, marginLeft: "auto", flex: "0 0 auto" }}>
                            <button className="btn sm ghost" type="button" aria-label="Move up"
                                    disabled={i === 0} onClick={e => { e.preventDefault(); moveCol(i, -1); }}>
                              <ArrowUp size={12} /></button>
                            <button className="btn sm ghost" type="button" aria-label="Move down"
                                    disabled={i === v.columns.length - 1}
                                    onClick={e => { e.preventDefault(); moveCol(i, 1); }}>
                              <ArrowDown size={12} /></button>
                            <button className="btn sm ghost" type="button" aria-label="Remove column"
                                    disabled={c.required}
                                    title={c.required ? "Mandatory in the Lloyd's standard" : "Remove this column"}
                                    onClick={e => { e.preventDefault();
                                      change({ columns: v.columns.filter((_, j) => j !== i) }); }}>
                              <X size={12} /></button>
                          </span>
                        )}
                      </label>
                      {editable && c.why === "Added by you" && (
                        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6 }}>
                          <span className="sub" style={{ flex: "0 0 auto" }}>Filled from</span>
                          <select value={fillValue(c.key)} style={{ maxWidth: 320 }}
                                  onChange={e => setCol(i, { key: e.target.value === MAP
                                    ? (c.key.startsWith("field:") || c.key.startsWith("col:") ? c.key : "")
                                    : e.target.value })}>
                            <option value={MAP}>An input column — chosen on Field Mapping</option>
                            {fillOpts.map(g => (
                              <optgroup key={g.group} label={g.group}>
                                {g.opts.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
                              </optgroup>
                            ))}
                          </select>
                        </div>
                      )}
                      {c.why && (
                        <div className="sub" style={{ marginTop: 3, fontSize: 11.5 }}>{c.why}</div>
                      )}
                      {(c.required || c.added) && (
                        <div style={{ marginTop: 3 }}>
                          {c.required && <span className="badge b-info" style={{ fontSize: 10.5 }}>Mandatory</span>}
                          {c.added && <span className="badge b-ok" style={{ fontSize: 10.5 }}>Reinsurance</span>}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
                {shown.length === 0 && (
                  <tr><td className="sub">No column matches.</td></tr>
                )}
              </tbody>
            </table>
          </div>
          {editable && (adding ? (
            <div className="card pad" style={{ marginTop: 10, boxShadow: "none" }}>
              <div style={{ display: "flex", gap: 10, alignItems: "flex-end", flexWrap: "wrap" }}>
                <div className="field" style={{ marginBottom: 0, flex: "1 1 220px" }}>
                  <label>Heading in the file</label>
                  <input autoFocus value={newHeading} onChange={e => setNewHeading(e.target.value)}
                         placeholder="e.g. Risk location"
                         onKeyDown={e => { if (e.key === "Enter") addColumn(); }} />
                </div>
                <div className="field" style={{ marginBottom: 0, flex: "1 1 260px" }}>
                  <label>Filled from</label>
                  <select value={newKey || MAP} onChange={e => setNewKey(e.target.value)}>
                    <option value={MAP}>An input column — chosen on Field Mapping</option>
                    {fillOpts.map(g => (
                      <optgroup key={g.group} label={g.group}>
                        {g.opts.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
                      </optgroup>
                    ))}
                  </select>
                </div>
                <button className="btn pri" type="button" disabled={!newHeading.trim()} onClick={addColumn}>
                  Add column
                </button>
                <button className="btn" type="button" onClick={() => setAdding(false)}>Cancel</button>
              </div>
            </div>
          ) : (
            <button className="btn sm" type="button" style={{ marginTop: 10 }}
                    onClick={() => { setAdding(true); setNewHeading(""); setNewKey(""); }}>
              <Plus size={12} /> Add a column
            </button>
          ))}
          {(source === "standard" || source === "contract") && (
            <div className="seg sm" style={{ marginTop: 12 }}>
              {(["xlsx", "csv"] as const).map(f => (
                <button key={f} type="button" className={v.file_format === f ? "on" : ""}
                        onClick={() => change({ file_format: f })}>
                  {f === "xlsx" ? "Excel" : "CSV"}
                </button>
              ))}
            </div>
          )}
        </>
      )}
      {bad("columns")}
    </div>
  );
}
