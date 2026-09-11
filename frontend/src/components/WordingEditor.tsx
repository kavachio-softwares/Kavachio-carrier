/**
 * The contract wording editor — words you type, and chips you cannot.
 *
 * A section body is STORED as tokens (`{{commission_max_pct}}`) so that
 * changing a term in step 1 moves the sentence and the check together. But
 * nobody should ever SEE a token: `{{commission_max_pct}}` in a lawyer's
 * screen is a developer artefact leaking into a contract.
 *
 * So the two representations are kept apart:
 *
 *   stored     "…commission not exceeding {{commission_max_pct}} of premium…"
 *   shown      "…commission not exceeding [ 15% ] of premium…"
 *
 * where the bracketed part is a chip: one atomic, non-editable object with the
 * live value in it. You can delete it or type around it; you cannot edit its
 * insides, and you cannot accidentally produce one by typing "15%" — which is
 * the whole point. A hand-typed 15% stays 15% when the cap moves to 12.5%, and
 * that is how a contract ends up saying one thing while the system checks
 * another.
 *
 * The serialisation is the delicate part and it runs one way only: the DOM is
 * the truth while the user is typing, and it is walked back into tokens on
 * every input. The value prop is NOT written back into the DOM on each
 * keystroke — doing that moves the caret to the start of the line on every
 * character. It is only re-rendered when the section changes underneath
 * (a different section picked, or the wording regenerated), which `sectionKey`
 * signals.
 */
import { useEffect, useRef, useState } from "react";

/** Tokens → HTML, for putting into the editable div.
 *
 *  `editable` marks the chips whose term can be changed from here, which is
 *  what makes them clickable and what the hover state is for. */
function toHtml(body: string, tokens: Record<string, string>,
                labels: Record<string, string>,
                editable?: (token: string) => boolean): string {
  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return esc(body)
    .replace(/\{\{([a-z_]+)\}\}/g, (_m, key: string) => {
      const shown = tokens[key];
      if (shown == null) {
        // A token with no value behind it — the term was cleared in step 1.
        // Shown as a gap rather than silently dropped: a sentence with a hole
        // in it is a problem somebody can see and fix.
        return `<span class="term term-missing" contenteditable="false" `
             + `data-token="${key}" title="This term is not set in step 1">`
             + `${esc(labels[key] ?? key)} — not set</span>`;
      }
      const may = !editable || editable(key);
      return `<span class="term" contenteditable="false" data-token="${key}" `
           + (may ? `data-edit="1" ` : "")
           + `title="${esc(labels[key] ?? key)} — `
           + (may ? "click to change it, here and everywhere else it appears"
                  : "set on Terms, and it changes with it")
           + `">${esc(shown)}</span>`;
    })
    .replace(/\n/g, "<br>");
}

/** The DOM back into tokens. Walks the tree so a chip anywhere — including one
 *  the browser has wrapped in its own markup after a paste — still serialises
 *  as its token rather than as the text it happens to display. */
function toTokens(root: HTMLElement): string {
  let out = "";
  const walk = (node: Node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      out += node.textContent ?? "";
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as HTMLElement;
    const token = el.dataset?.token;
    if (token) { out += `{{${token}}}`; return; }
    if (el.tagName === "BR") { out += "\n"; return; }
    const block = /^(DIV|P)$/.test(el.tagName);
    if (block && out && !out.endsWith("\n")) out += "\n";
    el.childNodes.forEach(walk);
    if (block && !out.endsWith("\n")) out += "\n";
  };
  root.childNodes.forEach(walk);
  return out.replace(/\n{3,}/g, "\n\n").trimEnd();
}

export function WordingEditor({
  sectionKey, body, tokens, labels, onChange, onInsertRequest, onChipsEdited,
  onChipValue, chipEditable,
}: {
  /** Changes when a DIFFERENT section is being edited, or the wording was
   *  regenerated — the only times the DOM should be rebuilt from `body`. */
  sectionKey: string;
  body: string;
  tokens: Record<string, string>;
  labels: Record<string, string>;
  onChange: (body: string) => void;
  /** Registers an inserter the toolbar can call, so a chip lands at the caret
   *  rather than always at the end. */
  onInsertRequest?: (insert: (token: string) => void) => void;
  /** The section as it was when editing began, and as it is now — on blur, and
   *  only when they differ. The caller works out whether a chip was typed over
   *  and whether the term should follow (see utils/wordingEdits).
   *
   *  ON BLUR, not on every keystroke: half a number is not a term. "1" on the
   *  way to "15" would set the commission to 1% and put the chip back over it,
   *  which is a fight with the person typing. */
  onChipsEdited?: (before: string, after: string) => void;
  /** A chip was edited IN PLACE — clicked, and a new value typed into the small
   *  box that opens over it. The caller decides whether that value is one the
   *  term can take; the chip itself is never rewritten here, because the chip
   *  is not text. It shows a term, and the term is what changes. */
  onChipValue?: (token: string, text: string) => void;
  /** Whether a chip can be edited that way. A commission can; the carrier's
   *  name cannot — it is not a term of this contract, it is who you are. */
  chipEditable?: (token: string) => boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  // The chip being edited in place, and where to float its box.
  const [editing, setEditing] = useState<
    { token: string; top: number; left: number; value: string } | null>(null);
  // The body as it stood when this section was last written into the DOM.
  const baseline = useRef<string>(body);
  // The VALUES the chips in this section were last drawn with. A term can move
  // while the editor is open — typing over a chip is an edit to the term — and
  // without this the guard below would skip the rebuild (the body is our own
  // echo) and leave every chip showing the number it used to have.
  const lastValues = useRef<string>("");
  // A chip's value changed while the caret is in here. Redrawing now would
  // throw the caret to the start of the line mid-sentence, so it waits for the
  // typing to stop.
  const owed = useRef(false);
  const [redrawTick, setRedrawTick] = useState(0);
  // What we last wrote out, so an echo of our own value never rebuilds the DOM
  // mid-type and throws the caret to the start.
  const lastEmitted = useRef<string>("");

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    // Only what THIS section quotes: a term changing elsewhere is not a reason
    // to rebuild the DOM under somebody's caret.
    const values = [...(body.matchAll(/\{\{([a-z_]+)\}\}/g))]
      .map(m => `${m[1]}=${tokens[m[1]] ?? ""}`).join("|");
    if (body === lastEmitted.current && values === lastValues.current) return;
    if (body === lastEmitted.current && document.activeElement === el) {
      owed.current = true;
      return;
    }
    el.innerHTML = toHtml(body, tokens, labels, chipEditable);
    lastEmitted.current = body;
    lastValues.current = values;
    baseline.current = body;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sectionKey, body, tokens, chipEditable, redrawTick]);

  useEffect(() => {
    if (!onInsertRequest) return;
    onInsertRequest((token: string) => {
      const el = ref.current;
      if (!el) return;
      el.focus();
      const sel = window.getSelection();
      const chip = document.createElement("span");
      chip.className = tokens[token] == null ? "term term-missing" : "term";
      chip.contentEditable = "false";
      chip.dataset.token = token;
      chip.textContent = tokens[token] ?? `${labels[token] ?? token} — not set`;

      if (sel && sel.rangeCount && el.contains(sel.anchorNode)) {
        const range = sel.getRangeAt(0);
        range.deleteContents();
        range.insertNode(chip);
        // A space after, so the next thing typed is not swallowed into the chip.
        const spacer = document.createTextNode(" ");
        chip.after(spacer);
        range.setStartAfter(spacer);
        range.collapse(true);
        sel.removeAllRanges();
        sel.addRange(range);
      } else {
        el.append(chip, document.createTextNode(" "));
      }
      const next = toTokens(el);
      lastEmitted.current = next;
      onChange(next);
    });
  }, [onInsertRequest, tokens, labels, onChange]);

  /** Clicking a chip opens a small box over it with the value in it. The chip
   *  stays atomic — it is not turned into editable text — because what is being
   *  changed is the TERM, and a term is a value, not a run of characters
   *  somebody may leave half-typed. */
  function openChip(target: EventTarget | null) {
    if (!onChipValue) return;
    const chip = (target as HTMLElement | null)?.closest?.(".term") as
      HTMLElement | null;
    const token = chip?.dataset?.token;
    if (!chip || !token || !wrap.current) return;
    if (chipEditable && !chipEditable(token)) return;
    const box = wrap.current.getBoundingClientRect();
    const at = chip.getBoundingClientRect();
    setEditing({
      token,
      top: at.bottom - box.top + 4,
      left: Math.max(0, at.left - box.left),
      // Seeded with what the chip SHOWS — "11%", not 11 — because that is what
      // the person clicked on, and the reader that parses it takes either.
      value: tokens[token] ?? "",
    });
  }

  return (
    <div ref={wrap} style={{ position: "relative" }}>
    <div
      ref={ref}
      className="wording"
      // A contentEditable has no placeholder of its own, so the prompt is
      // carried as an attribute and drawn by CSS while the box is empty (see
      // .wording:empty::before). It used to be the section's VALUE — which
      // meant "Write this section in your own words." was real text, and went
      // into the contract whenever nobody replaced it.
      data-placeholder="Write this clause in your own words, or insert a term from the right."
      contentEditable
      spellCheck={false}
      suppressContentEditableWarning
      onInput={() => {
        const el = ref.current;
        if (!el) return;
        const next = toTokens(el);
        lastEmitted.current = next;
        onChange(next);
      }}
      onBlur={() => {
        const el = ref.current;
        if (!el) return;
        // A value moved while they were typing — draw it now they have stopped.
        // First, and whether or not anybody is listening below: the chip on the
        // screen is showing a number the contract no longer carries.
        if (owed.current) { owed.current = false; setRedrawTick(n => n + 1); }
        if (!onChipsEdited) return;
        const now = toTokens(el);
        const was = baseline.current;
        baseline.current = now;
        if (was !== now) onChipsEdited(was, now);
      }}
      onClick={e => openChip(e.target)}
      onPaste={e => {
        // Plain text only. A paste carrying markup could otherwise bring in
        // something that LOOKS like a chip — styled, bold, the right colour —
        // and is just text that will never follow its term.
        e.preventDefault();
        const text = e.clipboardData.getData("text/plain");
        document.execCommand("insertText", false, text);
      }}
    />

    {editing && (
      <div
        style={{ position: "absolute", top: editing.top, left: editing.left,
                 zIndex: 30 }}
        className="flex items-center gap-1.5 rounded-lg border border-border
                   bg-white px-2 py-1.5 shadow-lg"
      >
        <input
          autoFocus
          defaultValue={editing.value}
          aria-label={`${labels[editing.token] ?? editing.token} — the agreed value`}
          className="w-[130px] rounded-md border border-border px-2 py-1 text-[13px]
                     text-ink outline-none focus:border-[#077282]"
          onKeyDown={e => {
            if (e.key === "Escape") { e.preventDefault(); setEditing(null); }
            if (e.key === "Enter") {
              e.preventDefault();
              const v = (e.target as HTMLInputElement).value;
              setEditing(null);
              onChipValue?.(editing.token, v);
            }
          }}
          onBlur={e => {
            const v = e.target.value;
            setEditing(null);
            onChipValue?.(editing.token, v);
          }}
        />
        <span className="whitespace-nowrap text-[11px] text-ink-soft">
          changes the term
        </span>
      </div>
    )}
    </div>
  );
}
