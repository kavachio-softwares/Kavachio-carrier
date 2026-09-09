/**
 * What somebody did to a chip, and what it means for the term behind it.
 *
 * A chip in the wording IS the term: it shows the live value, and the sentence
 * and the check move together because both read the same one. The editor makes
 * a chip impossible to type by hand — but you can delete one and type over it,
 * and on the screen the result looks identical. It is not identical. The
 * sentence then says 15% while the check still enforces 11, and nothing about
 * the page admits it.
 *
 * The banner on that screen promises this works one way ("change the commission
 * cap and every one of them updates"). This is the other way: type a value where
 * a chip stood and THE TERM MOVES, which puts the chip back, moves the check,
 * and leaves one answer to the question instead of two.
 *
 * How the slot is found: the body before the edit is turned into a pattern —
 * its literal text kept exactly, each `{{token}}` an open slot — and the body
 * after is matched against it. Whatever is standing in a slot is what happened
 * to that chip. If the sentence was rewritten enough that the pattern no longer
 * matches, nothing is claimed at all: better to leave the words alone than to
 * guess which term a rewritten sentence meant.
 */

const TOKEN = /\{\{([a-z_]+)\}\}/g;

/** What now stands where a chip stood. */
export type ChipEdit = { token: string; text: string };

/** A term that should follow the words. `matched` is the exact run of text the
 *  chip goes back over, so the prose typed around it survives. */
export type TermMove = { token: string; value: string; matched: string };

/** The shape of one agreed term — as much of the served spec as this needs. */
export type TermShape = {
  kind: "percent" | "money" | "int" | "choice" | "text";
  choices?: string[] | null;
};

type Split = { literals: string[]; tokens: string[] };

function split(body: string): Split {
  const literals: string[] = [];
  const tokens: string[] = [];
  let last = 0;
  for (const m of body.matchAll(TOKEN)) {
    literals.push(body.slice(last, m.index));
    tokens.push(m[1]);
    last = (m.index ?? 0) + m[0].length;
  }
  literals.push(body.slice(last));
  return { literals, tokens };
}

/** The slots of `before`, filled by whatever `after` has in them — or null when
 *  the sentence changed too much for the question to have an answer. */
function slots(before: string, after: string): string[] | null {
  const { literals, tokens } = split(before);
  if (!tokens.length) return null;
  const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(
    "^" + literals.map(esc).join("([\\s\\S]*?)") + "$");
  const m = pattern.exec(after);
  return m ? m.slice(1) : null;
}

/** Every chip that no longer stands where it stood. */
export function chipEdits(before: string, after: string): ChipEdit[] {
  if (before === after) return [];
  const filled = slots(before, after);
  if (!filled) return [];
  const { tokens } = split(before);
  const out: ChipEdit[] = [];
  tokens.forEach((token, i) => {
    const text = filled[i] ?? "";
    // The chip is still standing there — words were added around it, which is
    // ordinary editing and nothing to do with the term.
    if (text.includes(`{{${token}}}`)) return;
    out.push({ token, text });
  });
  return out;
}

/** Numbers standing on their own, so "15% of premium" gives one answer and
 *  "between 10% and 15%" gives two — and two is not an answer. */
function onlyNumber(text: string, pattern: RegExp): string | null {
  const found = [...text.matchAll(pattern)];
  return found.length === 1 ? found[0][0] : null;
}

/**
 * The term this edit is asking for, or null to leave the words exactly as
 * typed.
 *
 * Null is the common case and the safe one: a chip deleted outright, replaced
 * with prose, or replaced with something that could be two different numbers.
 * Those show up instead as "the wording does not quote this term" on the review
 * — said out loud rather than guessed at.
 */
export function readTermMove(edit: ChipEdit, term: TermShape): TermMove | null {
  const text = edit.text.trim();
  if (!text || text.includes("{{")) return null;

  const make = (value: string, matched: string): TermMove | null =>
    value ? { token: edit.token, value, matched } : null;

  if (term.kind === "percent") {
    const hit = onlyNumber(text, /-?\d+(?:\.\d+)?\s*%/g)
             ?? onlyNumber(text, /-?\d+(?:\.\d+)?/g);
    return hit ? make(hit.replace(/[%\s]/g, ""), hit) : null;
  }
  if (term.kind === "money") {
    const hit = onlyNumber(text, /-?\d[\d,]*(?:\.\d+)?/g);
    return hit ? make(hit.replace(/,/g, ""), hit) : null;
  }
  if (term.kind === "int") {
    const hit = onlyNumber(text, /-?\d[\d,]*/g);
    return hit ? make(hit.replace(/,/g, ""), hit) : null;
  }
  if (term.kind === "choice") {
    const pick = (term.choices ?? []).find(
      c => c.toLowerCase() === text.toLowerCase());
    return pick ? make(pick, text) : null;
  }
  // Free text — a territory, a coverage. What was typed IS the term.
  return make(text, text);
}

/** The edited body with the moved chips put back over their new values, and
 *  every other word left exactly as it was typed. */
export function restoreChips(before: string, after: string,
                             moves: TermMove[]): string {
  if (!moves.length) return after;
  const filled = slots(before, after);
  if (!filled) return after;
  const { literals, tokens } = split(before);
  const by = new Map(moves.map(m => [m.token, m]));

  let out = "";
  tokens.forEach((token, i) => {
    out += literals[i];
    const text = filled[i] ?? "";
    const move = by.get(token);
    if (!move) { out += text; return; }
    const at = text.indexOf(move.matched);
    out += at < 0
      ? `{{${token}}}`
      : text.slice(0, at) + `{{${token}}}` + text.slice(at + move.matched.length);
  });
  return out + literals[literals.length - 1];
}
