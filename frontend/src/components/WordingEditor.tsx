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
import { useEffect, useRef } from "react";

/** Tokens → HTML, for putting into the editable div. */
function toHtml(body: string, tokens: Record<string, string>,
                labels: Record<string, string>): string {
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
      return `<span class="term" contenteditable="false" data-token="${key}" `
           + `title="${esc(labels[key] ?? key)} — set in step 1, changes with it">`
           + `${esc(shown)}</span>`;
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
  sectionKey, body, tokens, labels, onChange, onInsertRequest,
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
}) {
  const ref = useRef<HTMLDivElement>(null);
  // What we last wrote out, so an echo of our own value never rebuilds the DOM
  // mid-type and throws the caret to the start.
  const lastEmitted = useRef<string>("");

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (body === lastEmitted.current) return;
    el.innerHTML = toHtml(body, tokens, labels);
    lastEmitted.current = body;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sectionKey, body, tokens]);

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

  return (
    <div
      ref={ref}
      className="wording"
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
      onPaste={e => {
        // Plain text only. A paste carrying markup could otherwise bring in
        // something that LOOKS like a chip — styled, bold, the right colour —
        // and is just text that will never follow its term.
        e.preventDefault();
        const text = e.clipboardData.getData("text/plain");
        document.execCommand("insertText", false, text);
      }}
    />
  );
}
