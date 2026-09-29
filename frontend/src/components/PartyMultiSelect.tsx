import { useEffect, useRef, useState } from "react";
import { Search, ChevronDown, Check, X } from "lucide-react";

type Party = { id: number; legal_name: string };

// Styled multi-select for parties (carriers on File Submissions, brokers on
// Programmes): a pill-style trigger that opens a searchable checkbox list.
// Relies on the .proto tokens — render it inside a .proto wrapper.
export function PartyMultiSelect({
  parties, selected, onChange, noun, nounPlural,
}: {
  parties: Party[];
  selected: number[];
  onChange: (ids: number[]) => void;
  /** Capitalised singular, e.g. "Carrier". */
  noun: string;
  nounPlural: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  // Close when clicking outside.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const selectedSet = new Set(selected);
  const toggle = (id: number) =>
    onChange(selectedSet.has(id) ? selected.filter(x => x !== id) : [...selected, id]);

  const needle = q.trim().toLowerCase();
  const visible = needle
    ? parties.filter(c => c.legal_name.toLowerCase().includes(needle))
    : parties;

  const lower = nounPlural.toLowerCase();
  const label =
    selected.length === 0 ? `All ${nounPlural}`
    : selected.length === 1 ? (parties.find(c => c.id === selected[0])?.legal_name ?? `1 ${noun}`)
    : `${selected.length} ${nounPlural}`;

  return (
    <div className="ms" ref={ref}>
      <button type="button" className={`ms-trigger${selected.length ? " active" : ""}`}
        onClick={() => setOpen(o => !o)}>
        <span className="ms-label">{label}</span>
        {selected.length > 0 && (
          <span className="ms-clear" role="button" aria-label={`Clear ${lower}`}
            onClick={e => { e.stopPropagation(); onChange([]); }}>
            <X size={13} />
          </span>
        )}
        <ChevronDown size={15} className="ms-caret" />
      </button>

      {open && (
        <div className="ms-pop">
          <div className="ms-search">
            <Search size={14} />
            <input autoFocus placeholder={`Search ${lower}…`} value={q}
              onChange={e => setQ(e.target.value)} />
          </div>
          <div className="ms-list">
            {visible.length === 0 ? (
              <div className="ms-empty">No {lower} match.</div>
            ) : visible.map(c => {
              const on = selectedSet.has(c.id);
              return (
                <label key={c.id} className={`ms-opt${on ? " on" : ""}`}>
                  <input type="checkbox" checked={on} onChange={() => toggle(c.id)} />
                  <span className="ms-box">{on && <Check size={12} strokeWidth={3} />}</span>
                  <span className="ms-name">{c.legal_name}</span>
                </label>
              );
            })}
          </div>
          {selected.length > 0 && (
            <div className="ms-foot">
              <span className="linkish" onClick={() => onChange([])}>Clear Selection</span>
              <span className="muted">{selected.length} Selected</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
