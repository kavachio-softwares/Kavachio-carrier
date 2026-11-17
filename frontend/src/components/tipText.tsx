import { Fragment } from "react";

/**
 * Lays an ⓘ tooltip's text out for reading at a glance, so no tooltip has to
 * format itself (both InfoTip components use this):
 *
 *  - every sentence is its own short paragraph, with a gap between them —
 *    a run-on block of sentences was hard to scan;
 *  - the first sentence is the lead (what the thing is), in bold, when more
 *    follows;
 *  - a "Click … / Open … / Hover …" sentence is the action, set apart at the
 *    bottom under a thin rule;
 *  - a line starting "• " or "- " is a bullet.
 *
 * Writers keep writing plain sentences (and `\n` where they want a break).
 */

// A full stop after these is not the end of a sentence.
const ABBREV = /(?:\b(?:e\.g|i\.e|etc|vs|no|nos|mr|mrs|ms|dr|st|approx|incl|ref)\.)$/i;
// Sentence end: . ! ? (optionally followed by a closing quote/bracket), then a
// space, then something that starts a sentence.
const BOUNDARY = /([.!?]["”’)]?)\s+(?=[A-Z0-9"“‘(])/g;
const ACTION = /^(click|tap|hover|open|pick|choose|use|select|drag|press)\b/i;

export type TipBlock = { kind: "lead" | "text" | "action" | "bullet"; text: string };

function sentences(line: string): string[] {
  const out: string[] = [];
  let start = 0;
  for (const m of line.matchAll(BOUNDARY)) {
    const end = (m.index ?? 0) + m[1].length;
    const piece = line.slice(start, end).trim();
    if (ABBREV.test(piece)) continue;           // "e.g. Lloyd's" is one sentence
    if (piece) out.push(piece);
    start = end;
  }
  const rest = line.slice(start).trim();
  if (rest) out.push(rest);
  return out;
}

export function tipBlocks(text: string): TipBlock[] {
  const blocks: TipBlock[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const bullet = line.match(/^[•\-–]\s+(.*)$/);
    if (bullet) { blocks.push({ kind: "bullet", text: bullet[1] }); continue; }
    for (const s of sentences(line)) {
      blocks.push({ kind: ACTION.test(s) ? "action" : "text", text: s });
    }
  }
  // Actions read last, whatever order they were written in.
  const ordered = [...blocks.filter(b => b.kind !== "action"),
                   ...blocks.filter(b => b.kind === "action")];
  if (ordered.length > 1 && ordered[0].kind === "text") ordered[0] = { ...ordered[0], kind: "lead" };
  return ordered;
}

/** The laid-out body of a tooltip bubble (dark background, light text). */
export function TipBody({ text }: { text: string }) {
  const blocks = tipBlocks(text);
  const firstAction = blocks.findIndex(b => b.kind === "action");
  return (
    <span className="tip-body">
      {blocks.map((b, i) => (
        <Fragment key={i}>
          {i === firstAction && i > 0 && <span className="tip-rule" aria-hidden="true" />}
          <span className={`tip-${b.kind}`}>{b.text}</span>
        </Fragment>
      ))}
    </span>
  );
}
