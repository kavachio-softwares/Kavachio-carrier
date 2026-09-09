/**
 * The Output BDX Template field builder.
 *
 * Everything about the blueprint that generation needs but the mapping view
 * never showed: what each column is called in the file, what feeds it, what
 * type it holds, whether it may be left out, and what order it comes in.
 *
 * TWO RULES DO ALL THE WORK HERE.
 *
 * A rename changes the DISPLAY NAME only. Every field keeps an internal key
 * minted from its original heading, and the source mapping hangs off that key —
 * so calling "Insurer Name" something else never quietly detaches it from
 * `insurer_name`. The key is shown, greyed, next to the name for exactly that
 * reason.
 *
 * A field the reporting standard demands cannot be removed or made optional.
 * The server enforces it too (a browser is not a permission), but showing the
 * lock here means the user finds out while they are editing rather than when
 * they try to save.
 *
 * Saving re-validates, and a template with outstanding errors can be SAVED but
 * not ACTIVATED — the edit loop has to be able to pass through an invalid state
 * on its way to a valid one.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, ArrowDown, ArrowUp, CheckCircle2, Lock, Plus, RotateCcw,
  Trash2, FileWarning,
} from "lucide-react";
import Card from "./ui/Card";
import Button from "./ui/Button";
import { Field, Select, TextInput } from "./ui/Field";
import {
  addTemplateField, getTemplateFields, saveTemplateFields,
  type FieldsDoc, type TemplateField, type ValidationReport,
} from "../api/outputTemplate";
import { errText } from "../utils/directSetup";

export default function OutputTemplateFields({
  templateId, onSaved, refreshKey, blockedReason = null,
}: {
  templateId: number;
  onSaved?: () => void;
  /** Bumped by the sheet grid above when it changes a column, so this list
   *  re-reads instead of showing the template as it was a moment ago. */
  refreshKey?: number;
  /** Why saving is not allowed right now, or null when it is.
   *
   *  This list and the column-mapping rows above it both write the same value —
   *  `source_field` here is `canonical_field` there. This one works from a copy
   *  it loaded when it mounted, so saving it while the page holds unsaved
   *  mapping edits writes the pre-edit value back and loses them silently. The
   *  page knows when that is true; it says so here rather than letting the save
   *  happen and reporting success for work it undid. */
  blockedReason?: string | null;
}) {
  const [doc, setDoc] = useState<FieldsDoc | null>(null);
  const [fields, setFields] = useState<TemplateField[]>([]);
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [sheet, setSheet] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newName, setNewName] = useState("");

  const load = useCallback(() => {
    getTemplateFields(templateId)
      .then(d => {
        setDoc(d); setFields(d.fields); setReport(d.validation);
        setSheet(prev => prev || d.sheets[0] || "");
      })
      .catch(e => setErr(errText(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateId, refreshKey]);
  useEffect(load, [load]);

  const shown = useMemo(
    () => fields.filter(f => !sheet || f.sheet === sheet),
    [fields, sheet]);

  function patch(key: string, sheetName: string, change: Partial<TemplateField>) {
    setFields(prev => prev.map(f =>
      f.field_key === key && f.sheet === sheetName ? { ...f, ...change } : f));
    setMsg(null);
  }

  /** Move a field one place within its own sheet, renumbering only that sheet. */
  function move(key: string, sheetName: string, delta: -1 | 1) {
    setFields(prev => {
      const same = prev.filter(f => f.sheet === sheetName)
        .sort((a, b) => a.display_order - b.display_order);
      const i = same.findIndex(f => f.field_key === key);
      const j = i + delta;
      if (i < 0 || j < 0 || j >= same.length) return prev;
      [same[i], same[j]] = [same[j], same[i]];
      const order = new Map(same.map((f, n) => [f.field_key, n]));
      return prev.map(f => f.sheet === sheetName && order.has(f.field_key)
        ? { ...f, display_order: order.get(f.field_key)! } : f);
    });
    setMsg(null);
  }

  async function save(activate: boolean) {
    setBusy(true); setErr(null); setMsg(null);
    try {
      const r = await saveTemplateFields(templateId, fields, activate);
      setFields(r.fields); setReport(r.validation);
      setDoc(prev => prev ? { ...prev, template: r.template } : prev);
      setMsg(r.versioned
        ? `Saved as version ${r.template.version} — the version already used to `
          + `generate a file was left untouched.`
        : activate ? "Saved and activated." : "Saved.");
      onSaved?.();
    } catch (e: unknown) { setErr(errText(e)); }
    finally { setBusy(false); }
  }

  async function add() {
    if (!newName.trim()) return;
    setBusy(true); setErr(null);
    try {
      const r = await addTemplateField(templateId, {
        sheet, display_name: newName.trim(), data_type: "string",
      });
      setFields(r.fields); setNewName(""); setAdding(false);
      setMsg(`Added "${newName.trim()}" — save to keep it.`);
    } catch (e: unknown) { setErr(errText(e)); }
    finally { setBusy(false); }
  }

  if (!doc) {
    return <Card><div className="text-sm text-ink-muted">
      {err ?? "Loading the field list…"}</div></Card>;
  }

  const removed = fields.filter(f => !f.active).length;

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <h3 className="text-sm font-semibold">Output BDX Fields</h3>
        <span className="text-xs text-ink-muted">
          {shown.filter(f => f.active).length} in the file
          {removed > 0 && <> · {removed} removed</>}
        </span>
        {doc.standard?.standard && (
          <span className="pill text-[11px]">
            {doc.standard.standard}
            {doc.standard.jurisdiction ? ` · ${doc.standard.jurisdiction}` : ""}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {doc.sheets.length > 1 && (
            <Select className="!py-1 !w-auto" value={sheet}
              onChange={e => setSheet(e.target.value)}>
              {doc.sheets.map(s => <option key={s} value={s}>{s}</option>)}
            </Select>
          )}
          <Button variant="secondary" disabled={busy}
            onClick={() => setAdding(a => !a)}>
            <Plus size={14} /> Add Field
          </Button>
          <Button variant="secondary" disabled={busy || !!blockedReason}
            title={blockedReason ?? undefined}
            onClick={() => save(false)}>
            Save
          </Button>
          <Button disabled={busy || !!blockedReason || !report?.valid}
            onClick={() => save(true)}
            title={blockedReason
              ?? (report?.valid ? undefined
                  : "Fix the errors below before activating this template")}>
            Save &amp; Activate
          </Button>
        </div>
      </div>

      {/* Said out loud, not just as a tooltip on a greyed-out button — a
          disabled control with no reason beside it is the same dead end that
          made the silent overwrite hard to spot in the first place. */}
      {blockedReason && (
        <Note tone="warn"><AlertTriangle size={14} /> {blockedReason}</Note>
      )}
      {doc.locked_by_history && (
        <Note tone="info">
          A file has already been generated from version {doc.template.version}.
          Saving will create version {doc.template.version + 1} and leave this one
          exactly as it is, so the files already delivered keep describing
          themselves correctly.
        </Note>
      )}
      {err && <Note tone="warn"><AlertTriangle size={14} /> {err}</Note>}
      {msg && <Note tone="ok"><CheckCircle2 size={14} /> {msg}</Note>}
      <UnfilledRequired doc={doc} fields={fields} />
      {report && <ValidationPanel report={report} />}

      {adding && (
        <div className="mt-3 flex items-end gap-2 flex-wrap">
          <div className="min-w-[240px]">
            <Field label={`New field on "${sheet}"`}>
              <TextInput autoFocus value={newName} placeholder="e.g. Reinsurance Limit"
                onChange={e => setNewName(e.target.value)}
                onKeyDown={e => { if (e.key === "Enter") add(); }} />
            </Field>
          </div>
          <Button onClick={add} disabled={busy || !newName.trim()}>Add</Button>
          <Button variant="ghost" onClick={() => { setAdding(false); setNewName(""); }}>
            Cancel
          </Button>
        </div>
      )}

      {/* Fixed layout with declared widths: without them the field-name column
          absorbs the row and the source mapping — the thing most worth reading —
          gets squeezed to four characters. Scrolls sideways on a narrow screen
          rather than crushing the columns further. */}
      <div className="mt-3 overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-[12.5px] table-fixed min-w-[1040px]">
          <colgroup>
            <col className="w-[68px]" /><col className="w-[26%]" />
            <col className="w-[19%]" /><col className="w-[140px]" />
            <col className="w-[110px]" /><col className="w-[112px]" />
            <col className="w-[170px]" /><col className="w-[52px]" />
          </colgroup>
          <thead className="bg-surface-2 sticky top-0">
            <tr>
              <th className="text-left px-2 py-2">Order</th>
              <th className="text-left px-3 py-2">Field name</th>
              <th className="text-left px-3 py-2">Source field</th>
              <th className="text-left px-3 py-2">Where from</th>
              <th className="text-left px-3 py-2">Type</th>
              <th className="text-left px-3 py-2">Required</th>
              <th className="text-left px-3 py-2">Default / transform</th>
              <th className="text-left px-3 py-2"></th>
            </tr>
          </thead>
          <tbody>
            {shown.sort((a, b) => a.display_order - b.display_order).map((f, i) => (
              <Row key={`${f.sheet}||${f.field_key}`} f={f} index={i}
                sourceTypes={doc.source_types} dataTypes={doc.data_types}
                onPatch={c => patch(f.field_key, f.sheet, c)}
                onMove={d => move(f.field_key, f.sheet, d)} />
            ))}
          </tbody>
        </table>
        {shown.length === 0 && (
          <div className="p-4 text-sm text-ink-muted">
            This sheet has no fields yet.
          </div>
        )}
      </div>

      <p className="text-[11px] text-ink-soft mt-2">
        Renaming a field changes only what the column is called in the delivered
        file — the key beside it, and everything mapped to it, stay as they are.
        Fields the reporting standard marks mandatory are locked.
      </p>
    </Card>
  );
}

function Row({ f, index, sourceTypes, dataTypes, onPatch, onMove }: {
  f: TemplateField; index: number;
  sourceTypes: string[]; dataTypes: string[];
  onPatch: (c: Partial<TemplateField>) => void;
  onMove: (d: -1 | 1) => void;
}) {
  return (
    <tr className={`border-t border-border align-top ${f.active ? "" : "opacity-50"}`}>
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-0.5">
          <span className="text-ink-soft tabular-nums w-5 text-right">{index + 1}</span>
          <button className="text-ink-soft hover:text-ink p-0.5" title="Move up"
            onClick={() => onMove(-1)}><ArrowUp size={12} /></button>
          <button className="text-ink-soft hover:text-ink p-0.5" title="Move down"
            onClick={() => onMove(1)}><ArrowDown size={12} /></button>
        </div>
      </td>
      <td className="px-3 py-1.5">
        <TextInput className="!py-1 !text-[12.5px]" value={f.display_name}
          onChange={e => onPatch({ display_name: e.target.value })} />
        <div className="text-[10.5px] text-ink-soft mt-0.5 flex items-center
          gap-1.5 flex-wrap">
          <code className="truncate max-w-[60%]">{f.field_key}</code>
          {f.standard_ref && <span className="pill text-[10px]">{f.standard_ref}</span>}
          {f.category && <span>{f.category}</span>}
        </div>
      </td>
      <td className="px-3 py-1.5">
        <TextInput className="!py-1 !text-[12.5px] font-mono"
          value={f.source_field ?? ""} placeholder="not mapped"
          onChange={e => onPatch({ source_field: e.target.value || null })} />
      </td>
      <td className="px-3 py-1.5">
        <Select className="!py-1 !text-[12px]" value={f.source_type ?? ""}
          onChange={e => onPatch({ source_type: e.target.value })}>
          {sourceTypes.map(s => (
            <option key={s} value={s}>{s.replace("_", " ").toLowerCase()}</option>
          ))}
        </Select>
      </td>
      <td className="px-3 py-1.5">
        <Select className="!py-1 !text-[12px]" value={f.data_type ?? "string"}
          onChange={e => onPatch({ data_type: e.target.value })}>
          {dataTypes.map(d => <option key={d} value={d}>{d}</option>)}
        </Select>
      </td>
      <td className="px-3 py-1.5">
        {f.system_required ? (
          <span className="flex items-center gap-1 text-[11.5px] text-ink-muted"
            title="The reporting standard demands this field">
            <Lock size={11} /> required
          </span>
        ) : (
          <label className="flex items-center gap-1.5 text-[11.5px]">
            <input type="checkbox" checked={f.required}
              onChange={e => onPatch({ required: e.target.checked })} />
            {f.conditional ? "conditional" : "required"}
          </label>
        )}
      </td>
      <td className="px-3 py-1.5 space-y-1">
        <TextInput className="!py-1 !text-[12px]" value={f.default_value ?? ""}
          placeholder="default value"
          onChange={e => onPatch({ default_value: e.target.value || null })} />
        <TextInput className="!py-1 !text-[12px]" value={f.transformation_rule ?? ""}
          placeholder="transform"
          onChange={e => onPatch({ transformation_rule: e.target.value || null })} />
      </td>
      <td className="px-3 py-1.5">
        {f.active ? (
          <button
            className={`p-1 ${f.system_required
              ? "text-ink-soft cursor-not-allowed" : "text-red-400 hover:text-red-600"}`}
            disabled={f.system_required}
            title={f.system_required
              ? "The reporting standard requires this field, so it cannot be removed"
              : "Remove from the output"}
            onClick={() => onPatch({ active: false })}>
            <Trash2 size={13} />
          </button>
        ) : (
          <button className="p-1 text-ink-soft hover:text-ink" title="Put it back"
            onClick={() => onPatch({ active: true })}>
            <RotateCcw size={13} />
          </button>
        )}
      </td>
    </tr>
  );
}

/**
 * Required columns that nothing in the bordereau can fill.
 *
 * Recorded when the template was built from both sides — the published
 * requirements and the incoming file — and shown HERE rather than in the dialog
 * that created it. In the dialog it was one line in a modal on the way to a
 * button; here there is room to name the columns, say why they stayed in, and
 * put the fix (a constant, a contract term, a system value) one row away in the
 * editor below.
 *
 * Silent on a template built before the check existed: no answer is not the
 * same as a clean answer.
 */
function UnfilledRequired({ doc, fields }: {
  doc: FieldsDoc; fields: TemplateField[];
}) {
  const check = doc.source_check;
  if (!check?.checked_input) return null;
  // Re-read against the CURRENT field list, and only for columns whose value
  // can ONLY come from the bordereau. A required column pointed at the carrier's
  // own name or the contract's dates is filled at generation from records the
  // platform already holds — reporting those as missing would train people to
  // ignore the report. A default value or a removal since the template was
  // built settles it too.
  const flagged = (check.unresolved_required ?? []).map(name =>
    fields.find(x => x.column_name === name || x.display_name === name))
    .filter((f): f is TemplateField =>
      !!f && f.active && f.required && !f.default_value && !f.input_match);
  const still = flagged.filter(f => !f.source_field || f.source_type === "BDX_DATA");
  const elsewhere = flagged.length - still.length;
  if (!still.length) {
    return (
      <Note tone="ok">
        <CheckCircle2 size={14} />
        Every required column here has something to fill it.
      </Note>
    );
  }
  const one = still.length === 1;
  return (
    <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3
      text-[12px] text-amber-800">
      <div className="flex items-center gap-1.5 font-medium">
        <FileWarning size={14} />
        {still.length} required column{one ? "" : "s"} will come out empty
      </div>
      <p className="mt-1 leading-relaxed">
        The standard demands {one ? "this column" : "these columns"} and expects
        {one ? " its" : " their"} value to come from the bordereau — but the file
        you built this from carries nothing that matches
        {one ? " it" : " them"}. {one ? "It stays" : "They stay"} in the layout,
        because dropping a mandatory column is not the fix. Give
        {one ? " it" : " each"} a default value in the row below, point
        {one ? " it" : " them"} at a contract, party or system field instead, or
        leave {one ? "it" : "them"} for the data to catch up.
        {elsewhere > 0 && (
          <> {elsewhere} other required column{elsewhere === 1 ? " is" : "s are"}{" "}
            filled from records the platform already holds, so
            {elsewhere === 1 ? " it is" : " they are"} not listed here.</>
        )}
      </p>
      <ul className="mt-2 flex flex-wrap gap-1.5">
        {still.map(f => (
          <li key={f.field_key} title={f.source_field
              ? `mapped to ${f.source_field}, which nothing in the bordereau feeds`
              : "no source field set"}
            className="rounded-full bg-white/70 border border-amber-300
              px-2 py-0.5 text-[11.5px]">
            {f.display_name || f.column_name}
          </li>
        ))}
      </ul>
    </div>
  );
}

function ValidationPanel({ report }: { report: ValidationReport }) {
  if (report.valid && report.warning_count === 0) {
    return (
      <Note tone="ok">
        <CheckCircle2 size={14} /> Every check passed — this template is ready to
        activate.
      </Note>
    );
  }
  return (
    <div className="mt-3 rounded-md border border-border overflow-hidden">
      <div className="px-3 py-2 bg-surface-2 text-[12px] font-medium">
        {report.error_count > 0
          ? `${report.error_count} issue${report.error_count === 1 ? "" : "s"} to fix before this can be activated`
          : "Worth a look"}
        {report.warning_count > 0 && (
          <span className="text-ink-muted font-normal">
            {" "}· {report.warning_count} warning{report.warning_count === 1 ? "" : "s"}
          </span>
        )}
      </div>
      <ul className="divide-y divide-border">
        {report.findings.map((f, i) => (
          <li key={i} className="px-3 py-1.5 text-[12px] flex items-start gap-2">
            <span className={`mt-0.5 shrink-0 ${f.severity === "critical"
              ? "text-red-500" : "text-amber-500"}`}>
              <AlertTriangle size={13} />
            </span>
            <span>
              {f.message}
              {f.sheet && <span className="text-ink-soft"> · {f.sheet}</span>}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Note({ tone, children }: {
  tone: "ok" | "warn" | "info"; children: React.ReactNode;
}) {
  const cls = {
    ok: "border-emerald-200 bg-emerald-50 text-emerald-800",
    warn: "border-red-200 bg-red-50 text-red-700",
    info: "border-sky-200 bg-sky-50 text-sky-800",
  }[tone];
  return (
    <div className={`mt-3 rounded-md border px-3 py-2 text-[12px]
      flex items-start gap-2 ${cls}`}>
      {children}
    </div>
  );
}
