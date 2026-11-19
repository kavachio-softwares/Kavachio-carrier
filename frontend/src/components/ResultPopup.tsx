// What an action came to — "Sent for approval to 2 people", "That could not be
// saved: …" — in a popup the user closes, instead of a bar at the top of the
// screen that is easy to miss and stays after the moment has passed.
//
// Any screen calls showDone(message) or showError(errorOrMessage); <ResultPopup/>
// is mounted ONCE in App, beside GlobalErrorPopup, so it covers every screen —
// the reinsurer's private link included. Messages arriving while one is open
// wait their turn, one popup at a time.
//
// A request that failed with a 5xx or no response has already been shown by
// GlobalErrorPopup (api/client.ts marks it _friendlyHandled), so showError
// leaves it alone rather than stacking a second popup over the first.
//
// Only an action's RESULT belongs here. A field's own error stays beside the
// field, an error inside an open dialog stays in the dialog, and a note about
// how things stand ("Not used yet") stays on the page.

import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, X } from "lucide-react";
import { fieldErrors } from "../api/contractRecord";
import { Button } from "./ui/Button";

type Result = { kind: "done" | "error"; message: string; title: string };

const queue: Result[] = [];
const listeners = new Set<() => void>();
const changed = () => listeners.forEach(fn => fn());

/** A success: "Saved.", "Sent to 3 reinsurers." */
export function showDone(message: string, title = "Done") {
  if (!message?.trim()) return;
  queue.push({ kind: "done", message, title });
  changed();
}

/** A failure — a caught error (its server message is used) or a message.
 *  An error GlobalErrorPopup has already shown is not shown twice. */
export function showError(e: unknown, title = "That did not work") {
  if (e && typeof e === "object" && (e as { _friendlyHandled?: boolean })._friendlyHandled) return;
  const message = typeof e === "string" ? e : fieldErrors(e).message;
  if (!message?.trim()) return;
  queue.push({ kind: "error", message, title });
  changed();
}

export default function ResultPopup() {
  const [, tick] = useState(0);
  useEffect(() => {
    const fn = () => tick(n => n + 1);
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  }, []);
  const cur = queue[0];

  useEffect(() => {
    if (!cur) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!cur) return null;
  function close() {
    queue.shift();
    changed();
  }
  const done = cur.kind === "done";
  const Icon = done ? CheckCircle2 : AlertTriangle;

  return (
    // z-[1090]: above the loading overlay (z-1000), below GlobalErrorPopup
    // (z-1100), which reports a failure of the whole request.
    <div className="fixed inset-0 z-[1090] flex items-center justify-center p-4"
         role={done ? "dialog" : "alertdialog"} aria-modal="true" aria-label={cur.title}>
      <div className="absolute inset-0 bg-black/40" onClick={close} />
      <div className="relative z-10 w-full max-w-md rounded-xl bg-white shadow-xl border border-border">
        <button onClick={close} aria-label="Close"
          className="absolute top-3.5 right-3.5 text-ink-soft hover:text-ink transition">
          <X size={18} />
        </button>
        <div className="px-6 pt-6 pb-5 flex flex-col items-center text-center">
          <div className={`flex h-12 w-12 items-center justify-center rounded-full mb-3.5 ${
            done ? "bg-emerald-50 text-emerald-600" : "bg-danger/10 text-danger"}`}>
            <Icon size={24} strokeWidth={1.8} />
          </div>
          <h2 className="text-base font-semibold">{cur.title}</h2>
          <p className="text-sm text-ink-muted mt-1.5" style={{ whiteSpace: "pre-line" }}>
            {cur.message}
          </p>
        </div>
        <footer className="px-6 py-3.5 border-t border-border flex items-center justify-center">
          <Button autoFocus onClick={close} className="min-w-[120px]">OK</Button>
        </footer>
      </div>
    </div>
  );
}
