// Friendly app-wide error popup — the visual half of the safeguard in
// api/client.ts. Whenever any request fails unexpectedly (HTTP 5xx, server
// unreachable), the client publishes a notice and this popup appears with a
// plain-language message instead of the raw "Internal Server Error" text.
// Mounted ONCE in App (above the router) so it covers every screen, including
// login. Expected, page-handled errors (4xx validation messages, not-found)
// never trigger it — pages keep presenting those inline as before.

import { useEffect, useState } from "react";
import { CloudOff, X } from "lucide-react";
import { ApiErrorNotice, subscribeApiErrors } from "../api/client";
import { Button } from "./ui/Button";

export default function GlobalErrorPopup() {
  const [notice, setNotice] = useState<ApiErrorNotice | null>(null);

  useEffect(
    // Keep the FIRST notice while one is showing — a burst of failing requests
    // (e.g. a page firing several calls at once) shows one popup, not a
    // flickering stack.
    () => subscribeApiErrors((n) => setNotice((cur) => cur ?? n)),
    [],
  );

  useEffect(() => {
    if (!notice) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setNotice(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [notice]);

  if (!notice) return null;
  const close = () => setNotice(null);
  const title = notice.kind === "network" ? "Connection problem" : "Something went wrong";

  return (
    // z-[1100]: above the global loading overlay (.k-overlay, z-1000) so the
    // popup is never hidden behind a lingering spinner.
    <div className="fixed inset-0 z-[1100] flex items-center justify-center p-4" role="alertdialog" aria-modal="true" aria-label={title}>
      <div className="absolute inset-0 bg-black/40" onClick={close} />
      <div className="relative z-10 w-full max-w-md rounded-xl bg-white shadow-xl border border-border">
        <button onClick={close} aria-label="Close"
          className="absolute top-3.5 right-3.5 text-ink-soft hover:text-ink transition">
          <X size={18} />
        </button>
        <div className="px-6 pt-6 pb-5 flex flex-col items-center text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-danger/10 text-danger mb-3.5">
            <CloudOff size={24} strokeWidth={1.8} />
          </div>
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="text-sm text-ink-muted mt-1.5">{notice.message}</p>
          <p className="text-xs text-ink-soft mt-2">
            If this keeps happening, please contact your administrator.
          </p>
        </div>
        <footer className="px-6 py-3.5 border-t border-border flex items-center justify-center">
          <Button onClick={close} className="min-w-[120px]">Close</Button>
        </footer>
      </div>
    </div>
  );
}
