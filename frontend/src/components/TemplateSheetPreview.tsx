/**
 * The output template, shown as the spreadsheet it becomes.
 *
 * A template IS a spreadsheet layout, and until now the only way to read one
 * here was a vertical list of field rows — which answers "what are the fields?"
 * but not the question people actually arrive with: "what will the file LOOK
 * like?". Column order, how wide the thing runs, which headings sit next to
 * each other, which ones are mandatory: all of that is obvious in a grid and
 * invisible in a list.
 *
 * So this draws the real thing — column letters, row numbers, the header row,
 * and the sample values the template was learned from underneath. It also works
 * like one: the menu on a column letter inserts a column to its left or right,
 * or takes it out, exactly where a person is pointing rather than at the bottom
 * of a list somewhere else. Everything else about a column — its source field,
 * its type, its default — is still edited in the field builder below, and the
 * two are looking at the same data, so a change in either shows up in both.
 *
 * Only ACTIVE columns are drawn, in display order, under their display names —
 * the same three rules the file writers follow, so what is on screen is what
 * would be delivered.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Plus, Trash2, X } from "lucide-react";
import {
  addTemplateField, getTemplateFields, saveTemplateFields,
} from "../api/outputTemplate";
import { errText } from "../utils/directSetup";

export type PreviewColumn = {
  column_name: string;
  display_name?: string | null;
  samples?: unknown[] | null;
  required?: boolean | null;
  system_required?: boolean | null;
  active?: boolean | null;
  display_order?: number | null;
  column_index?: number | null;
  data_type?: string | null;
  input_match?: { column?: string | null; confidence?: number | null } | null;
};
export type PreviewSheet = { sheet_name: string; columns: PreviewColumn[] };

/** A, B, … Z, AA, AB — the spreadsheet's own column names. */
export function columnLetter(i: number): string {
  let s = "";
  for (let n = i; n >= 0; n = Math.floor(n / 26) - 1) {
    s = String.fromCharCode(65 + (n % 26)) + s;
  }
  return s;
}

const MIN_ROWS = 6;

export default function TemplateSheetPreview({
  sheets, maxRows = 12, title, templateId, onChanged,
}: {
  sheets: PreviewSheet[];
  /** How many sample rows to draw. The grid keeps its shape when there are none. */
  maxRows?: number;
  title?: string;
  /** Given both of these, the grid becomes editable: insert and remove columns
   *  in place. Without them it stays a read-only picture of the template. */
  templateId?: number;
  onChanged?: () => void;
}) {
  const names = sheets.map(s => s.sheet_name);
  const [active, setActive] = useState(names[0] ?? "");
  const sheet = sheets.find(s => s.sheet_name === active) ?? sheets[0];
  const editable = !!templateId && !!onChanged;

  // Which column's menu is open, what is being typed into it, and anything the
  // server refused. One at a time — a spreadsheet has one open menu.
  const [menu, setMenu] = useState<{ at: number; side: "left" | "right" } | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const wrap = useRef<HTMLDivElement>(null);

  useEffect(() => { setMenu(null); setErr(null); }, [active]);
  useEffect(() => {
    if (!menu) return;
    const away = (e: MouseEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setMenu(null);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [menu]);

  const cols = useMemo(() => {
    const live = (sheet?.columns ?? []).filter(c => c.active !== false);
    return [...live].sort((a, b) => order(a) - order(b));
  }, [sheet]);

  // The samples are stored per column — a spreadsheet is read by row, so they
  // are turned back into rows here. Columns hold different numbers of samples,
  // so a short one leaves a blank cell rather than shifting the row.
  const rows = useMemo(() => {
    const depth = Math.min(
      maxRows, Math.max(0, ...cols.map(c => (c.samples ?? []).length)));
    return Array.from({ length: depth }, (_, r) =>
      cols.map(c => {
        const v = (c.samples ?? [])[r];
        return v === undefined || v === null ? "" : String(v);
      }));
  }, [cols, maxRows]);

  async function insert(at: number) {
    const name = draft.trim();
    if (!templateId || !name) return;
    setBusy(true); setErr(null);
    try {
      await addTemplateField(templateId, {
        sheet: sheet!.sheet_name, display_name: name, position: at,
      });
      setMenu(null); setDraft("");
      onChanged?.();
    } catch (e: unknown) { setErr(errText(e)); }
    finally { setBusy(false); }
  }

  /** Take a column out of the file. Routed through the ordinary save so the
   *  server's own rules apply — notably that a column the reporting standard
   *  demands cannot be removed, and says so instead of quietly going. */
  async function remove(col: PreviewColumn) {
    if (!templateId) return;
    setBusy(true); setErr(null);
    try {
      const doc = await getTemplateFields(templateId);
      const next = doc.fields.map(f =>
        f.sheet === sheet!.sheet_name && f.column_name === col.column_name
          ? { ...f, active: false } : f);
      await saveTemplateFields(templateId, next, false);
      setMenu(null);
      onChanged?.();
    } catch (e: unknown) { setErr(errText(e)); }
    finally { setBusy(false); }
  }

  if (!sheet || !cols.length) {
    return (
      <div className="rounded-lg border border-border bg-surface-2 p-4 text-sm text-ink-muted">
        This template has no columns switched on yet.
      </div>
    );
  }

  const blanks = Math.max(0, MIN_ROWS - rows.length);

  return (
    <div ref={wrap} className="rounded-lg border border-border overflow-hidden bg-white">
      {title && (
        <div className="px-3 py-2 border-b border-border text-[12px] text-ink-muted">
          {title}
        </div>
      )}
      {err && (
        <div className="px-3 py-2 border-b border-border bg-red-50 text-[12px]
          text-red-700 flex items-start gap-2">
          <span className="flex-1">{err}</span>
          <button onClick={() => setErr(null)}><X size={13} /></button>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="sheet-grid border-collapse text-[12.5px] tabular-nums">
          <thead>
            {/* The spreadsheet's own column letters — the row a person uses to
                say "the value in column F is wrong". */}
            <tr className="bg-surface-2 text-ink-soft">
              <th className="sticky left-0 z-20 bg-surface-2 border border-border
                w-10 min-w-10 h-6" />
              {cols.map((c, i) => (
                <th key={`L${i}`}
                  className="relative border border-border font-normal text-[11px]
                    px-2 h-6 min-w-[150px] group">
                  {columnLetter(i)}
                  {editable && (
                    <button type="button" disabled={busy}
                      title={`Column ${columnLetter(i)} — insert or remove`}
                      onClick={() => {
                        setDraft(""); setErr(null);
                        setMenu(m => m?.at === i ? null : { at: i, side: "right" });
                      }}
                      className="absolute right-0.5 top-1/2 -translate-y-1/2 rounded
                        p-0.5 text-ink-soft opacity-0 group-hover:opacity-100
                        focus:opacity-100 hover:bg-surface-2 transition">
                      <ChevronDown size={12} />
                    </button>
                  )}
                  {editable && menu?.at === i && (
                    <ColumnMenu
                      letter={columnLetter(i)} column={c} busy={busy}
                      draft={draft} setDraft={setDraft}
                      onInsertLeft={() => insert(i)}
                      onInsertRight={() => insert(i + 1)}
                      onRemove={() => remove(c)}
                      onClose={() => setMenu(null)} />
                  )}
                </th>
              ))}
            </tr>
            {/* The header row the delivered file carries. */}
            <tr className="bg-white">
              <th className="sticky left-0 z-20 bg-surface-2 border border-border
                text-[11px] font-normal text-ink-soft w-10 min-w-10" />
              {cols.map((c, i) => (
                <th key={`H${i}`} title={headerTitle(c)}
                  className="border border-border px-2 py-2 font-semibold
                    text-ink text-center align-middle min-w-[150px]">
                  <span className="whitespace-nowrap">{header(c)}</span>
                  {(c.required || c.system_required) && (
                    <span className="text-red-500 ml-0.5">*</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, r) => (
              <tr key={r}>
                <td className="sticky left-0 z-10 bg-surface-2 border border-border
                  text-[11px] text-ink-soft text-center w-10 min-w-10">
                  {r + 1}
                </td>
                {row.map((v, i) => (
                  <td key={i}
                    className="border border-border px-2 py-1.5 italic
                      text-ink-muted whitespace-nowrap max-w-[280px] truncate">
                    {v}
                  </td>
                ))}
              </tr>
            ))}
            {/* Empty rows so an unfilled template still reads as a spreadsheet
                rather than as a header with nothing under it. */}
            {Array.from({ length: blanks }, (_, r) => (
              <tr key={`b${r}`}>
                <td className="sticky left-0 z-10 bg-surface-2 border border-border
                  text-[11px] text-ink-soft text-center w-10 min-w-10">
                  {rows.length + r + 1}
                </td>
                {cols.map((_, i) => (
                  <td key={i} className="border border-border px-2 py-1.5 h-[30px]" />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2 border-t border-border bg-surface-2
        px-2 py-1.5">
        {names.map(n => (
          <button key={n} type="button" onClick={() => setActive(n)}
            className={`px-3 py-1 text-[12px] rounded-t-md border-b-2 transition
              ${n === (sheet?.sheet_name ?? "")
                ? "border-brand text-ink font-medium bg-white"
                : "border-transparent text-ink-muted hover:text-ink"}`}>
            {n}
          </button>
        ))}
        {editable && (
          <span className="text-[11px] text-ink-soft">
            Point at a column letter to insert or remove
          </span>
        )}
        <span className="ml-auto text-[11.5px] text-ink-soft pr-1">
          {cols.length} column{cols.length === 1 ? "" : "s"} ·{" "}
          {rows.length ? `${rows.length} sample row${rows.length === 1 ? "" : "s"}`
                       : "no records"}
        </span>
      </div>
    </div>
  );
}

/** The little menu a spreadsheet drops from a column letter. */
function ColumnMenu({ letter, column, busy, draft, setDraft,
                     onInsertLeft, onInsertRight, onRemove, onClose }: {
  letter: string; column: PreviewColumn; busy: boolean;
  draft: string; setDraft: (v: string) => void;
  onInsertLeft: () => void; onInsertRight: () => void;
  onRemove: () => void; onClose: () => void;
}) {
  const locked = !!column.system_required;
  return (
    <div className="absolute z-30 left-0 top-full mt-0.5 w-[268px] rounded-lg
      border border-border bg-white shadow-lg p-2.5 text-left font-normal
      normal-case tracking-normal">
      <div className="flex items-center gap-2 text-[11px] text-ink-soft mb-1.5">
        <span>Column {letter}</span>
        <span className="truncate flex-1 text-ink-muted">
          {column.display_name || column.column_name}
        </span>
        <button onClick={onClose}><X size={12} /></button>
      </div>
      <input autoFocus value={draft} disabled={busy}
        placeholder="New column name"
        onChange={e => setDraft(e.target.value)}
        onKeyDown={e => { if (e.key === "Enter" && draft.trim()) onInsertRight(); }}
        className="w-full rounded-md border border-border px-2 py-1
          text-[12.5px] mb-1.5" />
      <div className="flex gap-1.5">
        <button type="button" disabled={busy || !draft.trim()} onClick={onInsertLeft}
          className="flex-1 inline-flex items-center justify-center gap-1 rounded-md
            border border-border px-2 py-1 text-[12px] hover:bg-surface-2
            disabled:opacity-40">
          <Plus size={12} /> Insert left
        </button>
        <button type="button" disabled={busy || !draft.trim()} onClick={onInsertRight}
          className="flex-1 inline-flex items-center justify-center gap-1 rounded-md
            border border-border px-2 py-1 text-[12px] hover:bg-surface-2
            disabled:opacity-40">
          <Plus size={12} /> Insert right
        </button>
      </div>
      <button type="button" disabled={busy || locked} onClick={onRemove}
        title={locked ? "The reporting standard demands this column" : undefined}
        className="mt-1.5 w-full inline-flex items-center justify-center gap-1
          rounded-md border border-red-200 text-red-600 px-2 py-1 text-[12px]
          hover:bg-red-50 disabled:opacity-40 disabled:hover:bg-transparent">
        <Trash2 size={12} /> Remove this column
      </button>
      <p className="mt-1.5 text-[10.5px] text-ink-soft leading-snug">
        {locked
          ? "This column is mandatory in the reporting standard, so it cannot be removed."
          : "Removing takes it out of the file; it stays in the list below so you can put it back."}
      </p>
    </div>
  );
}

function order(c: PreviewColumn): number {
  return c.display_order ?? c.column_index ?? 0;
}
function header(c: PreviewColumn): string {
  return c.display_name || c.column_name || "—";
}
function headerTitle(c: PreviewColumn): string {
  const bits = [c.column_name && c.column_name !== header(c)
    ? `originally "${c.column_name}"` : "",
    c.data_type ? `type: ${c.data_type}` : "",
    c.system_required ? "required by the reporting standard"
      : c.required ? "required" : "",
    c.input_match?.column ? `filled from "${c.input_match.column}"` : ""];
  return bits.filter(Boolean).join(" · ");
}
