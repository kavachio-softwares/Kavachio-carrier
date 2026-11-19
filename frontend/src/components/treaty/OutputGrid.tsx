/**
 * The reinsurer's file as a spreadsheet — column letters, the headings it
 * will carry, and real rows from the latest period — as the broker setup's
 * Output BDX Template shows its file.
 */
type Cell = string | number | null | undefined;

/** A spreadsheet column letter: 0 → A, 26 → AA. */
export function colLetter(i: number): string {
  let s = "";
  for (let n = i + 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
  return s;
}

export default function OutputGrid({ headings, rows, sheet, period, footer }: {
  headings: string[];
  rows: Cell[][];
  sheet: string;
  period?: string | null;
  /** Extra controls in the footer, beside the sheet tab. */
  footer?: React.ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-[12.5px]">
          <thead>
            <tr className="bg-surface-2 text-[11px] text-ink-soft">
              <th className="w-10 border-b border-r border-border" />
              {headings.map((_, i) => (
                <th key={i} className="border-b border-r border-border px-2 py-1 font-normal">{colLetter(i)}</th>
              ))}
            </tr>
            <tr>
              <th className="border-b border-r border-border bg-surface-2" />
              {headings.map((h, i) => (
                <th key={i} className="whitespace-nowrap border-b border-r border-border bg-white px-3 py-2.5 text-[13px] font-semibold text-ink"
                    style={{ textTransform: "none", letterSpacing: "normal" }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from({ length: Math.max(rows.length, 1) + 1 }).map((_, r) => (
              <tr key={r}>
                <td className="border-b border-r border-border bg-surface-2 px-2 py-1.5 text-center text-[11px] text-ink-soft">{r + 1}</td>
                {headings.map((_, i) => {
                  const x = rows[r]?.[i];
                  return (
                    <td key={i} className="whitespace-nowrap border-b border-r border-border px-3 py-1.5 italic text-ink-muted">
                      {x == null || x === "" ? "" : typeof x === "number" ? x.toLocaleString() : String(x)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-2 bg-surface-2 px-3 py-1.5">
        <span className="rounded bg-white px-3 py-1 text-[12px] font-medium text-ink shadow-sm">{sheet}</span>
        {footer}
        <span className="ml-auto text-[11.5px] text-ink-muted">
          {headings.length} columns · {rows.length} sample row{rows.length === 1 ? "" : "s"}
          {period ? ` from ${period}` : ""}
        </span>
      </div>
    </div>
  );
}
