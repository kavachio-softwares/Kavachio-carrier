// "Why are you making this change?" — the optional reason kept with a change
// (task 48.1; backend change_history.py reads the X-Change-Reason header).
//
// Asked in ONE place instead of on every screen: a request interceptor on the
// shared api client, for the important edits listed in ASK below. The screens
// themselves are untouched — the save waits for the answer, then goes out with
// the reason (or without one: it is always optional, and Skip is one click).
// "Don't ask again" lasts until the browser tab is closed.
//
// Installed once by importing this file (main.tsx). The popup renders into its
// own root, so it needs nothing in App.
import { useEffect, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MessageSquareText, X } from "lucide-react";
import { api } from "../api/client";
import { Button } from "./ui/Button";

type Ask = { method: string; path: RegExp; what: string; skip?: (body: unknown) => boolean };

// Saves that only move signature boxes or name the signers are not edits of the
// contract's terms, and a contract date fix re-sent with "confirm the gap" is the
// same edit again — neither is asked twice.
const SIGNATURE_ONLY = (body: unknown) => {
  const keys = body && typeof body === "object" ? Object.keys(body as object) : [];
  return keys.length > 0 && keys.every(k => ["signers", "signature_layout"].includes(k));
};

const ASK: Ask[] = [
  { method: "put", path: /^\/programs\/\d+$/, what: "this programme" },
  { method: "put", path: /^\/parties\/\d+$/, what: "this company" },
  { method: "patch", path: /^\/contracts\/\d+$/, what: "this contract", skip: SIGNATURE_ONLY },
  { method: "put", path: /^\/pipelines\/\d+$/, what: "this Bordereau Setup" },
  { method: "put", path: /^\/output-template\/\d+\/fields$/, what: "these output template fields" },
  { method: "put", path: /^\/rule-library\/\d+$/, what: "this rule" },
  { method: "patch", path: /^\/rule-library\/\d+$/, what: "switching this rule on or off" },
  { method: "delete", path: /^\/rule-library\/\d+$/, what: "deleting this rule" },
  { method: "put", path: /^\/programs\/\d+\/rules\/\d+$/, what: "this programme rule" },
  { method: "post", path: /^\/programs\/\d+\/rules\/\d+\/retire$/, what: "retiring this programme rule" },
  { method: "delete", path: /^\/programs\/\d+\/rules\/\d+$/, what: "deleting this programme rule" },
  { method: "put", path: /^\/programs\/\d+\/contracts\/\d+\/rules\/\d+\/tolerance$/, what: "this rule's tolerance" },
  { method: "delete", path: /^\/programs\/\d+\/contracts\/\d+\/rules\/\d+$/, what: "removing this rule" },
];

const HEADER = "X-Change-Reason";
const QUIET_KEY = "kavachio.changeReason.quiet";
const REUSE_MS = 2 * 60 * 1000;
const recent = new Map<string, { reason: string; at: number }>();

function quiet(): boolean {
  try { return sessionStorage.getItem(QUIET_KEY) === "1"; } catch { return false; }
}
function setQuiet() {
  try { sessionStorage.setItem(QUIET_KEY, "1"); } catch { /* private window: ask again next time */ }
}

function pathOf(url: string | undefined): string {
  if (!url) return "";
  try {
    const u = new URL(url, "http://x");
    return u.pathname.replace(/\/+$/, "") || "/";
  } catch { return url.split("?")[0]; }
}

function bodyOf(data: unknown): unknown {
  if (typeof data !== "string") return data;
  try { return JSON.parse(data); } catch { return null; }
}

// ── the popup ───────────────────────────────────────────────────────────────
let root: Root | null = null;
let pending: { what: string; resolve: (r: string) => void } | null = null;
const waiting: { what: string; resolve: (r: string) => void }[] = [];
const listeners = new Set<() => void>();

function ask(what: string): Promise<string> {
  if (!root) {
    const el = document.createElement("div");
    el.id = "change-reason-root";
    document.body.appendChild(el);
    root = createRoot(el);
    root.render(<ReasonPopup />);
  }
  return new Promise(resolve => {
    waiting.push({ what, resolve });
    if (!pending) { pending = waiting.shift() ?? null; listeners.forEach(fn => fn()); }
  });
}

function answer(reason: string) {
  pending?.resolve(reason);
  pending = waiting.shift() ?? null;
  listeners.forEach(fn => fn());
}

function ReasonPopup() {
  const [, tick] = useState(0);
  const [text, setText] = useState("");
  const [dontAsk, setDontAsk] = useState(false);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const fn = () => { tick(n => n + 1); setText(""); setDontAsk(false); };
    listeners.add(fn);
    return () => { listeners.delete(fn); };
  }, []);
  useEffect(() => { if (pending) setTimeout(() => box.current?.focus(), 0); });
  if (!pending) return null;

  const done = (reason: string) => { if (dontAsk) setQuiet(); answer(reason.trim()); };
  return (
    // Above the loading overlay (z-1000) and the result popup (z-1090).
    <div className="fixed inset-0 z-[1095] flex items-center justify-center p-4" role="dialog" aria-modal="true"
      aria-label="Why are you making this change?"
      onKeyDown={e => {
        if (e.key === "Escape") done("");
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) done(text);
      }}>
      <div className="absolute inset-0 bg-black/40" onClick={() => done("")} />
      <div className="relative z-10 w-full max-w-md rounded-xl bg-white shadow-xl border border-border">
        <button onClick={() => done("")} aria-label="Skip"
          className="absolute top-3.5 right-3.5 text-ink-soft hover:text-ink transition"><X size={18} /></button>
        <div className="px-6 pt-6 pb-4">
          <div className="flex items-center gap-2.5 mb-2">
            <span className="flex h-9 w-9 items-center justify-center rounded-full bg-sky-50 text-sky-700">
              <MessageSquareText size={18} /></span>
            <h2 className="text-base font-semibold">Why are you changing {pending.what}?</h2>
          </div>
          <p className="text-sm text-ink-muted mb-3">
            Optional. It is kept with the change, so anyone reading Audit Logs → Change History later knows why.</p>
          <textarea ref={box} rows={3} maxLength={500} value={text} onChange={e => setText(e.target.value)}
            placeholder="e.g. Agreed with the broker on the 12 Nov call"
            className="w-full rounded-md border border-border px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-sky-200" />
          <label className="mt-2 flex items-center gap-2 text-xs text-ink-muted">
            <input type="checkbox" checked={dontAsk} onChange={e => setDontAsk(e.target.checked)} />
            Don't ask me again until I close this tab
          </label>
        </div>
        <footer className="px-6 py-3.5 border-t border-border flex items-center justify-end gap-2">
          <Button variant="secondary" onClick={() => done("")}>Skip</Button>
          <Button onClick={() => done(text)} disabled={!text.trim()}>Save with reason</Button>
        </footer>
      </div>
    </div>
  );
}

// ── the interceptor ─────────────────────────────────────────────────────────
api.interceptors.request.use(async cfg => {
  const method = (cfg.method || "get").toLowerCase();
  if (method === "get" || quiet()) return cfg;
  const path = pathOf(cfg.url);
  const rule = ASK.find(a => a.method === method && a.path.test(path));
  if (!rule || rule.skip?.(bodyOf(cfg.data))) return cfg;
  const key = `${method} ${path}`;
  const last = recent.get(key);
  let reason = last && Date.now() - last.at < REUSE_MS ? last.reason : null;
  if (reason === null) {
    reason = await ask(rule.what);
    recent.set(key, { reason, at: Date.now() });
  }
  if (reason) cfg.headers.set(HEADER, encodeURIComponent(reason));
  return cfg;
});
