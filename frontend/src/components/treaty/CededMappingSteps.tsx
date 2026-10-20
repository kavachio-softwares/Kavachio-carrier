/**
 * The input and the mapping steps of a reinsurance bordereau setup — the same
 * shape as a broker's setup: INPUT template → OUTPUT template → MAPPING.
 *
 *   Input template  what the statement is read from — the brokers'
 *                   bordereaux on the programmes the treaty protects (set up
 *                   on the broker side; listed, not changed), or a file of the
 *                   carrier's OWN, uploaded once as a sample.
 *   Field mapping   every column of the reinsurer's file, and the input column
 *                   that fills it — laid out as the broker setup's Field
 *                   Mapping & Validation Rules: Input column → Output column →
 *                   Treaty clause & rule.
 *
 * The carrier deals only in columns. A column copied across reads ONE input
 * column, by its heading (`col:<heading>`) — never "whichever column carries
 * the field", which picked the wrong one when several share it. Worked-out
 * figures (ceded premium, commission…) read data-model fields underneath;
 * picking their input column ties that field to it.
 */
import { useRef, useState } from "react";
import { Calculator, FileSpreadsheet, Loader2, Upload } from "lucide-react";
import { fieldErrors } from "../../api/contractRecord";
import { Combo } from "../DirectMappingWidgets";
import {
  uploadInputSample, type InputLayout, type SetupColumn, type SetupPreview,
  type SetupRecord, type SetupValues,
} from "../../api/ceded";

type OwnLayout = Extract<InputLayout, { source: "own" }>;
type OwnCol = OwnLayout["columns"][number];

/** The input side as it stands on screen: the carrier's own file when the
 *  setup reads one (even before it is saved), else the programmes' templates. */
function inputsOf(rec: SetupRecord, v: SetupValues) {
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  if (own) {
    return [{ template_id: 0, name: own.filename ?? "Your file", version: null,
              active: true, program_id: null, programme: "Your file", own: true,
              files: rec.options.inputs.find(t => t.own)?.files ?? 0,
              columns: own.columns.map(c => ({ name: c.name, field: c.field,
                                               sheet: own.sheet_name ?? null })) }];
  }
  return rec.options.inputs.filter(t => !t.own);
}

/** Each input column once — an own column can stand for several fields. */
function uniqueColumns(cols: { name: string; field: string | null }[]) {
  const by = new Map<string, string[]>();
  for (const c of cols) {
    const fs = by.get(c.name) ?? [];
    if (c.field) fs.push(c.field);
    by.set(c.name, fs);
  }
  return [...by.entries()].map(([name, fields]) => ({ name, fields }));
}

/** Which input fields the reinsurer's file reads. */
function usedFields(rec: SetupRecord, v: SetupValues) {
  const used = new Set<string>();
  for (const c of v.columns.filter(c => c.keep !== false)) {
    if (c.key.startsWith("field:")) used.add(c.key.slice(6));
    else if (!c.key.startsWith("col:"))
      for (const f of rec.options.derived[c.key]?.fields ?? []) used.add(f);
  }
  return used;
}

/** Input columns copied as themselves (`col:<heading>`). */
function usedNames(v: SetupValues) {
  return new Set(v.columns.filter(c => c.keep !== false && c.key.startsWith("col:"))
    .map(c => c.key.slice(4)));
}

// ── the input template ────────────────────────────────────────────────────
export function InputTemplateStep({ cid, rec, v, change, bad, listOnly = false, noColumns = false }: {
  cid: number; rec: SetupRecord; v: SetupValues;
  change: (patch: Partial<SetupValues>) => void;
  bad: (k: string) => React.ReactNode;
  /** Only the columns — the upload boxes above already choose and upload. */
  listOnly?: boolean;
  /** Only the choice and the upload — the columns are shown elsewhere. */
  noColumns?: boolean;
}) {
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  // Kept while switching to the brokers and back, so an upload is not lost.
  const held = useRef<InputLayout | null>(own);
  const used = usedFields(rec, v);
  const usedCols = usedNames(v);

  const inputs = own ? inputsOf(rec, v) : inputsOf(rec, v).filter(t =>
    v.program_ids == null || t.program_id == null || v.program_ids.includes(t.program_id));

  function pick(src: "brokers" | "own") {
    if (src === "brokers") {
      if (own) held.current = own;
      change({ input_layout: { source: "brokers" } });
    } else {
      change({ input_layout: held.current ?? { source: "own", columns: [] } });
    }
  }

  async function upload(f: File) {
    setBusy(true); setErr("");
    try {
      const lay = await uploadInputSample(cid, f);
      held.current = lay;
      change({ input_layout: lay });
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  const ColumnList = ({ cols }: { cols: { name: string; fields: string[] }[] }) => (
    <div className="tbl-wrap" style={{ maxHeight: 360, overflow: "auto" }}>
      <table className="panel-tbl">
        <thead><tr><th>Input column</th><th style={{ width: 170 }}>In the reinsurer&rsquo;s file</th></tr></thead>
        <tbody>
          {cols.map(c => (
            <tr key={c.name}>
              <td>{c.name}</td>
              <td>{usedCols.has(c.name) || c.fields.some(f => used.has(f))
                ? <span className="badge b-ok"><span className="d" />Mapped</span>
                : <span className="sub">not used</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );

  if (listOnly) {
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {bad("input_layout")}
        {own ? (
          own.columns.length ? <ColumnList cols={uniqueColumns(own.columns)} />
            : <div className="sub">Upload your input template above.</div>
        ) : !inputs.length ? (
          <div className="note warn">
            None of the chosen programmes has a bordereau template yet. Set up the
            broker&rsquo;s bordereau on the programme first — or upload your own
            input template above.
          </div>
        ) : inputs.map(t => (
          <div key={t.template_id}>
            <div className="sub" style={{ marginBottom: 4 }}>
              <b style={{ color: "var(--p-ink)" }}>{t.programme ?? t.name}</b> · {t.columns.length} columns ·{" "}
              {t.files} {t.files === 1 ? "bordereau" : "bordereaux"} received
            </div>
            <ColumnList cols={uniqueColumns(t.columns)} />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div className="seg" style={{ width: "fit-content" }}>
        <button type="button" className={own ? "" : "on"} onClick={() => pick("brokers")}>
          Your brokers&rsquo; bordereaux
        </button>
        <button type="button" className={own ? "on" : ""} onClick={() => pick("own")}>
          Your own file
        </button>
      </div>
      {bad("input_layout")}
      {err && <div className="note warn">{err}</div>}

      {own ? (
        <>
          <p className="sub" style={{ margin: 0 }}>
            Upload a sample of the file you keep — an Excel or CSV with a heading
            row. Its columns are what you map from on <b>Field Mapping</b>. Each
            period, upload that period&rsquo;s file on the Process Bordereau page.
          </p>
          <input ref={fileRef} type="file" accept=".xlsx,.xlsm,.csv" hidden
                 onChange={e => { const f = e.target.files?.[0]; e.target.value = ""; if (f) upload(f); }} />
          {busy ? (
            <div className="note" style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <Loader2 size={14} className="animate-spin" /> Reading your columns…
            </div>
          ) : own.columns.length === 0 ? (
            <button type="button" onClick={() => fileRef.current?.click()}
                    style={{ width: "100%", padding: "22px 16px", border: "1.5px dashed var(--p-border-2)",
                             borderRadius: 10, background: "transparent", cursor: "pointer", textAlign: "center" }}>
              <Upload size={18} />
              <div style={{ fontWeight: 600, marginTop: 6 }}>Upload your input template</div>
              <div className="sub">Excel or CSV — a heading row and a few rows are enough.</div>
            </button>
          ) : (
            <div className="card" style={{ boxShadow: "none" }}>
              <div className="card-h" style={{ flexWrap: "wrap", gap: 8 }}>
                <FileSpreadsheet size={16} className="ci" />
                <h3>{own.filename ?? "Your file"}</h3>
                <span className="sub">
                  {own.sheet_name ? `sheet “${own.sheet_name}” · ` : ""}
                  {uniqueColumns(own.columns).length} columns
                </span>
                <span className="right">
                  <button className="btn sm" type="button" onClick={() => fileRef.current?.click()}>
                    <Upload size={12} /> Replace
                  </button>
                </span>
              </div>
              {!noColumns && <ColumnList cols={uniqueColumns(own.columns)} />}
            </div>
          )}
        </>
      ) : !inputs.length ? (
        <div className="note warn">
          None of the chosen programmes has a bordereau template yet, so there is
          nothing to read from. Set up the broker&rsquo;s bordereau on the
          programme first — or use your own file.
        </div>
      ) : (
        <>
          <p className="sub" style={{ margin: 0 }}>
            The bordereaux your brokers send on the programmes this treaty
            protects. Their columns are set up on the broker side.
          </p>
          {!noColumns && inputs.map(t => (
            <div key={t.template_id} className="card" style={{ boxShadow: "none" }}>
              <div className="card-h" style={{ flexWrap: "wrap", gap: 8 }}>
                <FileSpreadsheet size={16} className="ci" />
                <h3>{t.programme ?? t.name}</h3>
                <span className="sub">{t.name} · {t.columns.length} columns</span>
                <span className="right">
                  <span className="badge b-mut">
                    {t.files} {t.files === 1 ? "bordereau" : "bordereaux"} received
                  </span>
                </span>
              </div>
              <ColumnList cols={uniqueColumns(t.columns)} />
            </div>
          ))}
        </>
      )}
    </div>
  );
}

// ── field mapping ─────────────────────────────────────────────────────────
/** "Treaty: N rule(s) across M output column(s)" — the line the broker setup
 *  puts between its Sheet Mapping and Field Mapping cards. */
export function treatyRuleLine(rec: SetupRecord, v: SetupValues): string {
  const cols = inputsOf(rec, v).flatMap(t => t.columns.map(c => ({ name: c.name, field: c.field })));
  const fieldsOf = (key: string): string[] => key.startsWith("field:") ? [key.slice(6)]
    : key.startsWith("col:") ? cols.filter(c => c.name === key.slice(4) && c.field).map(c => c.field as string)
    : key === "ceded_pct" ? [] : rec.options.derived[key]?.fields ?? [];
  const rules = (v.rules ?? []).filter(r => r.enabled);
  const covered = v.columns.filter(c => c.keep !== false && rules.some(r =>
    (rec.options.rule_fields?.[r.subject] ?? []).some(f => fieldsOf(c.key).includes(f)))).length;
  return `Treaty: ${rules.length} rule(s) across ${covered} output column(s).`;
}

export function FieldMappingStep({ rec, v, change, readOnly = false, hideStrip = false }: {
  rec: SetupRecord; v: SetupValues;
  /** Kept for the callers that pass it; the table no longer shows examples. */
  pv?: SetupPreview | null;
  change: (patch: Partial<SetupValues>) => void;
  /** The setup's read-only view: each input column named, not picked. */
  readOnly?: boolean;
  /** The rules line is shown by the page, between its cards. */
  hideStrip?: boolean;
}) {
  const own = v.input_layout?.source === "own" ? v.input_layout : null;
  const inputCols = uniqueColumns(inputsOf(rec, v).flatMap(t =>
    t.columns.map(c => ({ name: c.name, field: c.field }))));
  const kept = v.columns.map((c, i) => ({ c, i })).filter(x => x.c.keep !== false);
  const labelOf = (k: string) =>
    rec.options.columns.find(c => c.key === k)?.label
    ?? rec.options.context.find(c => c.key === k)?.label
    ?? (k === rec.options.share.key ? rec.options.share.label : k);
  const norm = (x: string) => x.toLowerCase().replace(/[^a-z0-9]+/g, "");

  /** The fields an output column reads, and whether it is worked out. */
  function needs(c: SetupColumn): { fields: string[]; how: string | null } {
    if (c.key.startsWith("field:")) return { fields: [c.key.slice(6)], how: null };
    // Copied from one exact input column.
    if (c.key.startsWith("col:")) return { fields: [], how: null };
    // The cession % is the treaty's, not a column's.
    if (c.key === "ceded_pct") return { fields: [], how: null };
    const d = rec.options.derived[c.key];
    if (!d) return { fields: [], how: null };
    return { fields: d.fields, how: d.how === "Copied" ? null : d.how };
  }

  /** The ONE input column filling it — named, or (of those sharing its field)
   *  the one headed like the output, else the first: the one read. */
  function currentInput(c: SetupColumn): string {
    if (c.key.startsWith("col:")) {
      const n = c.key.slice(4);
      return inputCols.some(ic => ic.name === n) ? n : "";
    }
    const { fields } = needs(c);
    const hits = inputCols.filter(ic => ic.fields.some(f => fields.includes(f)));
    return (hits.find(h => norm(h.name) === norm(c.heading)) ?? hits[0])?.name ?? "";
  }

  /** A copy column fills from one input column; a figure is worked out. */
  const isCalc = (c: SetupColumn) => !!needs(c).how;
  const isCtx = (c: SetupColumn) => !c.key.startsWith("field:") && !c.key.startsWith("col:")
    && needs(c).fields.length === 0 && !!c.key;

  /** Input column `name` fills output column `j` (null = fills nothing). The
   *  broker setup's way round: the input column is fixed, its output picked. */
  function assign(name: string, j: number | null) {
    // Off whatever copy column it filled before…
    let columns = v.columns.map(x => x.keep !== false && !isCtx(x) && !isCalc(x)
      && currentInput(x) === name ? { ...x, key: "" } : x);
    let input_layout = v.input_layout;
    if (j != null) {
      const c = columns[j];
      if (!isCalc(c)) {
        // …and onto the one picked — by its own heading, so a field several
        // columns share never picks the wrong one.
        columns = columns.map((x, k) => k === j ? { ...x, key: `col:${name}` } : x);
      } else if (own) {
        // A figure Kavachio works out reads a known field — that field now
        // comes from this column, and from no other: off every column that
        // had it (an extra entry goes, a column's own entry just loses it)…
        const f = needs(c).fields[0];
        const seen = new Set<string>();
        let cols: OwnCol[] = own.columns.flatMap(x => {
          const first = !seen.has(x.name);
          seen.add(x.name);
          if (x.field !== f) return [x];
          return first ? [{ ...x, field: null }] : [];
        });
        // …and onto this one, as an extra entry if it already carries one.
        const base = cols.find(x => x.name === name)!;
        cols = base.field
          ? [...cols, { name, column_index: base.column_index, field: f }]
          : cols.map(x => x === base ? { ...x, field: f } : x);
        input_layout = { ...own, columns: cols };
      }
    }
    change({ columns, input_layout });
  }

  // The treaty rules on what a column reads — shown beside it, as the broker
  // setup shows each column's contract clause and rule.
  const rulesOn = (c: SetupColumn) => {
    const fields = c.key.startsWith("col:")
      ? inputCols.find(ic => ic.name === c.key.slice(4))?.fields ?? [] : needs(c).fields;
    return (v.rules ?? []).filter(r => r.enabled
      && (rec.options.rule_fields?.[r.subject] ?? []).some(f => fields.includes(f)));
  };
  const ruleCols = kept.filter(({ c }) => rulesOn(c).length).length;
  const allMatched = kept.every(({ c }) => isCtx(c) || !!currentInput(c));
  // Brokers' templates decide what a figure reads; your own file can choose.
  const pickable = kept.filter(({ c }) => !isCtx(c) && (!isCalc(c) || !!own));
  const fedBy = new Map<string, { c: SetupColumn; i: number }[]>();
  for (const k of kept) {
    if (isCtx(k.c)) continue;
    const n = currentInput(k.c);
    if (n) fedBy.set(n, [...(fedBy.get(n) ?? []), k]);
  }
  // An input row shows the ONE output column picked against it; a figure
  // worked out from it (on the brokers' templates) gets a row of its own.
  const mainOf = (n: string) => {
    const feeds = fedBy.get(n) ?? [];
    return feeds.find(f => !isCalc(f.c)) ?? (own ? feeds.find(f => isCalc(f.c)) : undefined);
  };
  const mains = new Set(inputCols.map(ic => mainOf(ic.name)?.i));
  // Every other output column: worked out, from the treaty or the statement,
  // or left blank.
  const unfed = kept.filter(({ c, i }) =>
    isCtx(c) || !currentInput(c) || (isCalc(c) && !mains.has(i)));
  const GRID = "grid grid-cols-[minmax(0,1fr)_28px_minmax(0,1fr)_minmax(0,1.2fr)] gap-3";

  const Rules = ({ rules }: { rules: ReturnType<typeof rulesOn> }) => (
    <span className="grid min-h-[38px] content-center gap-1.5">
      {rules.length ? rules.map(r => (
        <span key={r.id} className="rounded-md border border-border bg-surface-2/60 px-2.5 py-1.5 text-[12px] leading-snug text-ink">
          <span className="mb-0.5 flex gap-2 text-[10px] font-bold uppercase">
            <span className={r.severity === "critical" ? "text-danger" : "text-amber-700"}>
              {r.severity === "critical" ? "Critical" : "Warning"}</span>
            <span className="font-semibold normal-case text-emerald-700">
              {r.source === "wording" ? "Treaty wording" : "Treaty terms"}</span>
          </span>
          {r.label}{r.clause ? <> — <i className="text-ink-muted">{r.clause}</i></> : null}
        </span>
      )) : (
        <span className="text-[12px] text-ink-soft">No treaty rule for this field.</span>
      )}
    </span>
  );
  const Chip = ({ label, tone, tip, calc = false }: { label: string; tone: string; tip: string; calc?: boolean }) => (
    <span title={tip}
      className={`inline-flex cursor-help items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ${tone}`}>
      {calc && <Calculator size={10} />}{label}
    </span>
  );
  const Box = ({ text }: { text: string }) => (
    <span className="flex min-h-[38px] items-center rounded-md border border-border bg-white px-3 text-ink">
      <span className="truncate" title={text}>{text}</span>
    </span>
  );

  return (
    <div>
      {!hideStrip && (
        <p className="mb-3 flex items-center gap-2 rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-ink-muted">
          Treaty: {(v.rules ?? []).filter(r => r.enabled).length} rule(s) across {ruleCols} output column(s).
        </p>
      )}
      {/* One card per sheet, as the broker setup: each input column → the
          output column it fills → the treaty clause & rule on it. */}
      <div className="overflow-hidden rounded-lg border border-border bg-white">
        <div className="flex items-center gap-2 px-4 py-3">
          <b className="text-[15px]">{v.layout?.sheet_name || "Lines"}</b>
          <span className={`ml-auto rounded-full px-2 py-0.5 text-[11px] font-semibold
            ${allMatched ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700"}`}>
            {allMatched ? "All Matched" : "Some columns left blank"}
          </span>
        </div>
        <div className={`${GRID} border-t border-border bg-surface-2 px-4 py-2.5 text-[11px] font-semibold uppercase tracking-wide text-ink-muted`}>
          <span>Input column</span><span /><span>Output column</span><span>Treaty clause &amp; rule</span>
        </div>

        {inputCols.map(ic => {
          const main = mainOf(ic.name);
          // Rarely, one input column is copied into two output columns.
          const rest = (fedBy.get(ic.name) ?? []).filter(f => f !== main && !isCalc(f.c));
          const rules = main ? rulesOn(main.c) : [];
          return (
            <div key={ic.name} className={`${GRID} items-start border-t border-border px-4 py-3 text-[13px]`}>
              <span className="flex min-h-[38px] min-w-0 items-center">
                <span className="truncate font-mono text-[12.5px] text-ink" title={ic.name}>{ic.name}</span>
              </span>
              <span className="flex min-h-[38px] items-center justify-center text-ink-soft">→</span>
              <span className="min-w-0 space-y-1">
                {readOnly
                  ? (main ? <Box text={main.c.heading} />
                      : <span className="flex min-h-[38px] items-center text-[12.5px] italic text-ink-soft">Not used</span>)
                  : <Combo value={main?.c.heading} options={pickable.map(p => p.c.heading)}
                      placeholder="Search output column…"
                      onSelect={h => assign(ic.name, h == null ? null
                        : pickable.find(p => p.c.heading === h)?.i ?? null)} />}
                {((main && isCalc(main.c)) || rest.length > 0) && (
                  <span className="flex flex-wrap gap-1">
                    {main && isCalc(main.c) && (
                      <Chip label="Calculated" calc tone="bg-violet-50 text-violet-700" tip={`Calculated: ${needs(main.c).how}`} />)}
                    {rest.map(f => (
                      <Chip key={f.i} label={`Also into ${f.c.heading}`}
                        tone="bg-emerald-50 text-emerald-700" tip={`Also copied into ${f.c.heading}`} />
                    ))}
                  </span>
                )}
              </span>
              <Rules rules={rules} />
            </div>
          );
        })}

        {unfed.length > 0 && (
          <div className="border-t border-border bg-surface-2/60 px-4 py-2 text-[11px] font-semibold uppercase tracking-wide text-ink-muted">
            Other output columns
          </div>
        )}
        {unfed.map(({ c, i }) => {
          const ctx = isCtx(c);
          const how = needs(c).how;
          const ownBlank = ctx && !!own && (c.key === "programme" || c.key === "broker");
          const from = !ctx && how ? currentInput(c) : "";
          const chip = from
            ? { label: `Calculated from ${from}`, tone: "bg-violet-50 text-violet-700", tip: `Calculated: ${how}` }
            : ownBlank
            ? { label: "Left blank", tone: "bg-amber-50 text-amber-700", tip: `Your own file has no ${c.key}` }
            : ctx
              ? { label: ["ceded_pct", "share_pct", "layer"].includes(c.key) ? "From the treaty"
                  : ["programme", "broker"].includes(c.key) ? "From the bordereau" : "From the statement",
                  tone: "bg-blue-50 text-blue-700",
                  tip: rec.options.derived[c.key]?.how ?? labelOf(c.key) }
              : how
                ? { label: "Left blank", tone: "bg-amber-50 text-amber-700",
                    tip: `Calculated: ${how} — but no input column carries what it needs` }
                : { label: "Left blank", tone: "bg-amber-50 text-amber-700",
                    tip: "No input column fills it — pick it against an input column above" };
          return (
            <div key={`o${i}`} className={`${GRID} items-start border-t border-border px-4 py-3 text-[13px]`}>
              <span className="flex min-h-[38px] items-center text-[12.5px] italic text-ink-soft">—</span>
              <span className="flex min-h-[38px] items-center justify-center text-ink-soft">→</span>
              <span className="min-w-0 space-y-1">
                <Box text={c.heading} />
                <Chip label={chip.label} tone={chip.tone} tip={chip.tip} calc={!!from} />
              </span>
              <Rules rules={rulesOn(c)} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
