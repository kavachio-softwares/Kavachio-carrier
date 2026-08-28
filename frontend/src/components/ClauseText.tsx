// Renders a contract-clause / rule-evidence string. When the clause contains a
// tabular block (the backend serializes detected tables as a GitHub-flavored
// Markdown grid — header row, a `| --- | --- |` separator, then data rows; see
// prompt_builder._render_table_markdown), we render a real <table> so tabular
// clauses are readable instead of a run of pipes. Any non-tabular clause (or a
// string we can't confidently parse) falls back to plain text unchanged.

type Block =
  | { kind: "text"; text: string }
  | { kind: "table"; header: string[]; rows: string[][] };

// A markdown separator cell: ---, :--, --:, :-:
function isSepCell(c: string): boolean {
  return /^:?-{2,}:?$/.test(c.trim());
}

// Split a "| a | b | c |" row into trimmed cells, tolerating a missing leading
// or trailing pipe.
function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

function isSepRow(line: string): boolean {
  const t = line.trim();
  if (!t.startsWith("|")) return false;
  const cells = splitRow(t);
  return cells.length > 0 && cells.every(isSepCell);
}

export function parseClauseBlocks(raw: string): Block[] {
  let text = raw.replace(/\r/g, "");
  // Defensive: strip the [TABLE]/[/TABLE] markers the extractor uses internally.
  text = text.replace(/\[\/?TABLE\]/g, "\n");

  // Safety net for the case where newlines were flattened to spaces upstream:
  // a single line that still carries a `| --- |` separator. Re-introduce row
  // breaks at the "| |" boundary between adjacent rows.
  if (!text.includes("\n") && /\|\s*:?-{2,}:?\s*\|/.test(text)) {
    text = text.replace(/\|\s+\|/g, "|\n|");
  }

  const lines = text.split("\n");
  const blocks: Block[] = [];
  let buf: string[] = [];

  const flushText = () => {
    const t = buf.join("\n").trim();
    if (t) blocks.push({ kind: "text", text: t });
    buf = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    const looksLikeRow = trimmed.startsWith("|") && trimmed.indexOf("|", 1) !== -1;
    // A table = a pipe row immediately followed by a separator row.
    if (looksLikeRow && isSepRow(lines[i + 1] ?? "")) {
      flushText();
      const header = splitRow(trimmed);
      const cols = header.length;
      i += 2; // consume header + separator
      const rows: string[][] = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = splitRow(lines[i]);
        while (cells.length < cols) cells.push("");
        rows.push(cells.slice(0, cols));
        i++;
      }
      i--; // the for-loop will re-increment
      blocks.push({ kind: "table", header, rows });
      continue;
    }
    buf.push(lines[i]);
  }
  flushText();
  return blocks;
}

export function ClauseText({ text, className }: { text?: string | null; className?: string }) {
  if (!text) return null;
  const blocks = parseClauseBlocks(text);
  const hasTable = blocks.some((b) => b.kind === "table");

  // No table detected → behave exactly as before (plain text).
  if (!hasTable) return <span className={className}>{text}</span>;

  return (
    <div className={className}>
      {blocks.map((b, i) =>
        b.kind === "text" ? (
          <p key={i} className="whitespace-pre-wrap">
            {b.text}
          </p>
        ) : (
          <div key={i} className="my-1 overflow-x-auto">
            <table className="w-full border-collapse text-[11px] leading-tight">
              <thead>
                <tr>
                  {b.header.map((h, j) => (
                    <th
                      key={j}
                      className="border border-navy/20 bg-navy/[0.06] px-2 py-1 text-left font-semibold whitespace-nowrap"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((r, ri) => (
                  <tr key={ri} className={ri % 2 ? "bg-navy/[0.03]" : undefined}>
                    {r.map((c, ci) => (
                      <td key={ci} className="border border-navy/15 px-2 py-1 align-top">
                        {c}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
}
