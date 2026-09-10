// Feature 10 — "Files".
//
// This screen used to be two: "How Files Arrive" at /intake and "Files Received"
// at /intake/arrivals, side by side in their own sidebar section. Each carried a
// button to the other — "Files received →" on one, "← How files arrive" on the
// other — which is the tell. When two screens each need a shortcut to the other,
// they are one screen.
//
// That merge went via a two-tab strip, and the strip was the wrong shape for it:
// one tab is opened every morning and the other about once a quarter, but they
// billed equally and the strip cost its height on every visit.
//
// So: the screen IS the Inbox. "Ways in" is a panel over it, reached from the
// page head. Setup is a thing you deliberately enter and leave — and because you
// never actually leave the queue, your filters, your scroll position and your
// ticked rows are all still there when you close it.
//
// The panel stays MOUNTED while closed (`.drawer.wide` hides it with
// `visibility`, which also keeps it out of the tab order). That is what lets the
// button carry a live route count and an attention dot, so on the ordinary day
// you can see intake is healthy without opening anything.
//
// The old paths still work — App.tsx redirects /intake to ?panel=ways and
// /intake/arrivals to the inbox — so every link already sent to a broker, and
// every link in an email, still lands somewhere sensible.
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Server } from "lucide-react";
import InboxTab from "./FilesReceived";
import WaysInTab, { AddRouteModal } from "./FilesArrive";

export default function Files() {
  // The panel lives in the URL so it is linkable and survives a reload —
  // Brokers reaches "give this broker a way in" with /files?panel=ways.
  const [params, setParams] = useSearchParams();
  const panelOpen = params.get("panel") === "ways";
  const setPanel = useCallback((open: boolean) => {
    setParams(prev => {
      const next = new URLSearchParams(prev);
      if (open) next.set("panel", "ways"); else next.delete("panel");
      return next;
    }, { replace: true });
  }, [setParams]);

  const [adding, setAdding] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  // Everything "Add a way in" needs, handed up by the panel's own fetch. The
  // dialog is rendered out here rather than inside the panel: a closed panel is
  // `visibility: hidden` and its children inherit that, so a dialog in there
  // could only ever show with the panel open behind it — which is what made one
  // click open two surfaces.
  const [addData, setAddData] = useState<React.ComponentProps<typeof AddRouteModal> | null>(null);
  // Reported up by the panel while it sits closed, so the button can say how
  // many ways in exist and whether one of them needs looking at.
  const [summary, setSummary] = useState<{ routes: number; needsAttention: number } | null>(null);
  // Settings / Add a way in / a collect result, open inside the panel.
  const [dialogOpen, setDialogOpen] = useState(false);

  const takeSummary = useCallback(
    (s: { routes: number; needsAttention: number }) => setSummary(s), []);
  const takeAddData = useCallback((d: Omit<React.ComponentProps<typeof AddRouteModal>,
    "open" | "onClose" | "onCreated">) =>
    setAddData(prev => ({ ...(prev ?? {} as never), ...d })), []);

  // Escape closes the panel, and the scrim behind it is clickable — the two
  // ways out people try first. NOT while a dialog is open inside it: Modal
  // listens on the window too, so one press would close the dialog and the
  // panel under it, and Escape should only ever close the innermost thing.
  useEffect(() => {
    if (!panelOpen || dialogOpen || adding) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setPanel(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panelOpen, dialogOpen, adding, setPanel]);

  return (
    <div className="proto">
      <section className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Files</h2>
            {/* The long explanation of what intake IS used to sit in a grey slab
                above the title on every visit, on both of the old screens,
                pushing the heading off the top of the viewport. It lives in the
                empty state and the reference block now, where somebody who does
                not know will actually be looking. */}
            <p>Every spreadsheet your brokers have sent, whichever way it came in,
              and what happened to it.</p>
          </div>
          <div className="actions">
            <button className="btn" aria-expanded={panelOpen} aria-controls="ways-panel"
              onClick={() => setPanel(true)}
              title="Where your brokers send their spreadsheets">
              <Server size={14} aria-hidden="true" />
              Ways in
              {summary && <span className="pcount">{summary.routes}</span>}
              {/* A way in that looks live and accepts nothing is the only thing
                  in here that ever needs somebody. Saying so on the closed
                  button is what makes not opening it a safe default. */}
              {!!summary?.needsAttention && <span className="pdot" aria-hidden="true" />}
            </button>
            <button className="btn" onClick={() => setRefreshKey(k => k + 1)}>Refresh</button>
            {/* Opens the dialog and nothing else. It used to open the panel too,
                because the dialog lived inside it. */}
            <button className="btn pri" onClick={() => setAdding(true)}>
              ＋ Add a way in</button>
          </div>
        </div>

        {/* The queue. Unchanged by the merge — this is the screen. */}
        <InboxTab active refreshKey={refreshKey} />
      </section>

      {/* ── Ways in ──
          Mounted whether or not it is open: the button above reads its summary,
          and reopening should not re-fetch a list you were halfway through. */}
      <div className={`scrim${panelOpen ? " on" : ""}`} onClick={() => setPanel(false)} />
      <aside id="ways-panel" className={`drawer wide${panelOpen ? " on" : ""}`}
        role="dialog" aria-modal="true" aria-hidden={!panelOpen} aria-label="Ways in">
        <div className="drawer-h">
          <div style={{ minWidth: 0 }}>
            <h4>Ways in</h4>
            <div className="ref" style={{ fontFamily: "inherit", fontSize: 12 }}>
              Where your brokers send their spreadsheets — set up once when a broker
              is onboarded, then rarely touched.
            </div>
          </div>
          <button type="button" className="closeb" aria-label="Close"
            onClick={() => setPanel(false)}>×</button>
        </div>

        <div className="drawer-b wayspanel">
          <WaysInTab refreshKey={refreshKey} onSummary={takeSummary}
            onDialogOpen={setDialogOpen} onAddData={takeAddData} />
        </div>

        <div className="drawer-f">
          <button className="btn pri" onClick={() => setAdding(true)}>＋ Add a way in</button>
          <button className="btn" style={{ marginLeft: "auto" }}
            onClick={() => setPanel(false)}>Done</button>
        </div>
      </aside>

      {/* Outside the panel on purpose — see addData above. Modal is z-50 and
          the panel z-41 in the same stacking context, so it sits over the panel
          when the panel happens to be open, and stands alone when it is not. */}
      <AddRouteModal open={adding}
        brokers={addData?.brokers ?? []}
        programmesByBroker={addData?.programmesByBroker ?? {}}
        emailsByBroker={addData?.emailsByBroker ?? {}}
        creatable={addData?.creatable ?? ["sftp"]}
        mailbox={addData?.mailbox ?? null}
        mailReady={addData?.mailReady ?? false}
        onClose={() => setAdding(false)}
        onCreated={() => { setAdding(false); setRefreshKey(k => k + 1); }} />
    </div>
  );
}
