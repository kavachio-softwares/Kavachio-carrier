// Audit Logs → "Contract or policy" filter (task 49.1, backend
// audit_record_filter.py): who did what to ONE record. The value is
// "contract:12" or "policy:P-1001" — "" for every record. ("file:march" still
// works from a link; the screen no longer offers it: it had no list to pick from.)
import { useEffect, useRef, useState } from "react";
import { getRecordChoices } from "../api/changeHistory";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

const KINDS = [["", "Any contract or policy"], ["contract", "One contract"], ["policy", "One policy"]] as const;

export function AuditRecordFilter({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  // A "file:…" value can still arrive from a link; the picker then reads "Any".
  const [kind, setKind] = useState(value.startsWith("file:") ? "" : value.split(":")[0] || "");
  const [text, setText] = useState(value.includes(":") ? value.slice(value.indexOf(":") + 1) : "");
  const [contracts, setContracts] = useState<{ id: number; name: string }[] | null>(null);
  const typed = useDebouncedValue(text, 400);
  // Set when THIS filter clears the value (switching contract → policy), so
  // that is not taken for Reset, which puts the picker back to "Any".
  const ownClear = useRef(false);

  // Cleared from outside (Reset, or the ✕ on its "Showing:" chip).
  useEffect(() => {
    if (value) return;
    if (ownClear.current) { ownClear.current = false; return; }
    setKind(""); setText("");
  }, [value]);
  useEffect(() => {
    if (kind === "contract" && contracts === null) getRecordChoices().then(r => setContracts(r.contracts)).catch(() => setContracts([]));
  }, [kind, contracts]);
  useEffect(() => {
    if (kind !== "policy") return;
    const next = typed.trim() ? `policy:${typed.trim()}` : "";
    if (next === value) return;
    if (!next) ownClear.current = true;      // the box emptied: still on "One policy"
    onChange(next);
  }, [typed]); // eslint-disable-line react-hooks/exhaustive-deps

  const pickKind = (k: string) => {
    setKind(k); setText("");
    if (value) { ownClear.current = true; onChange(""); }
  };
  return (
    <>
      <label className="fbar-field">
        <span className="sub">Contract or policy</span>
        <select className="fbar-select" aria-label="Filter by one record" value={kind} onChange={e => pickKind(e.target.value)}>
          {KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
      </label>
      {kind === "contract" && (
        <label className="fbar-field">
          <span className="sub">Contract</span>
          <select className="fbar-select" aria-label="Contract" value={value.startsWith("contract:") ? value.slice(9) : ""}
            onChange={e => onChange(e.target.value ? `contract:${e.target.value}` : "")}>
            <option value="">{contracts === null ? "Loading…" : "Pick a contract"}</option>
            {(contracts ?? []).map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </label>)}
      {kind === "policy" && (
        <label className="fbar-field">
          <span className="sub">Policy number</span>
          <span className="search">
            <input type="search" value={text} onChange={e => setText(e.target.value)}
              placeholder="Policy number, e.g. P-1001" aria-label="Policy number" />
          </span>
        </label>)}
    </>
  );
}
