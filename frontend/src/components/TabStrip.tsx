// One line of tabs, whatever how many: the row scrolls sideways (wheel, touch
// or the ‹ › buttons that appear only when some tabs are out of sight), and
// the chosen tab is always scrolled into view.
import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

export function TabStrip<K extends string>({ tabs, value, onChange }: {
  tabs: readonly { key: K; label: string }[]; value: K; onChange: (k: K) => void;
}) {
  const row = useRef<HTMLDivElement>(null);
  const [edge, setEdge] = useState({ left: false, right: false });

  const measure = () => {
    const el = row.current;
    if (!el) return;
    setEdge({ left: el.scrollLeft > 2, right: el.scrollLeft + el.clientWidth < el.scrollWidth - 2 });
  };
  useEffect(() => {
    measure();
    const el = row.current;
    if (!el) return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [tabs.length]);
  // The chosen tab in the middle of the row, once the layout (and the web
  // font, which changes every tab's width) has settled.
  useEffect(() => {
    const show = () => {
      const el = row.current;
      const tab = el?.querySelector<HTMLElement>(`[data-tab="${value}"]`);
      if (!el || !tab) return;
      el.scrollLeft = tab.offsetLeft - (el.clientWidth - tab.offsetWidth) / 2;   // the row is the tab's offsetParent
      measure();
    };
    const f = requestAnimationFrame(show);
    document.fonts?.ready.then(show).catch(() => {});
    return () => cancelAnimationFrame(f);
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps

  const move = (dir: 1 | -1) => row.current?.scrollBy({ left: dir * row.current.clientWidth * 0.7, behavior: "smooth" });
  const arrow = (dir: 1 | -1, show: boolean) => show && (
    <button type="button" aria-label={dir < 0 ? "Earlier tabs" : "More tabs"} onClick={() => move(dir)}
      style={{ flex: "none", background: "var(--p-surface, #fff)", border: "none", cursor: "pointer", padding: "0 4px",
               color: "var(--p-muted)", display: "grid", placeItems: "center" }}>
      {dir < 0 ? <ChevronLeft size={18} /> : <ChevronRight size={18} />}
    </button>);

  return (
    <div style={{ display: "flex", alignItems: "stretch", borderBottom: "1px solid var(--p-border)", marginBottom: 14 }}>
      {arrow(-1, edge.left)}
      <div ref={row} role="tablist" onScroll={measure} className="tab-strip"
        style={{ display: "flex", gap: 2, flexWrap: "nowrap", overflowX: "auto", scrollbarWidth: "none", flex: 1, minWidth: 0,
                 position: "relative" }}>
        {tabs.map(x => (
          <button key={x.key} data-tab={x.key} type="button" role="tab" aria-selected={value === x.key}
            onClick={() => onChange(x.key)}
            style={{ flex: "none", whiteSpace: "nowrap", background: "none", border: "none", padding: "8px 10px",
                     cursor: "pointer", fontSize: 13.5, fontWeight: value === x.key ? 700 : 500,
                     color: value === x.key ? "var(--p-brand, #077282)" : "var(--p-muted)",
                     borderBottom: `2px solid ${value === x.key ? "var(--p-brand, #077282)" : "transparent"}` }}>
            {x.label}</button>))}
      </div>
      {arrow(1, edge.right)}
    </div>
  );
}
