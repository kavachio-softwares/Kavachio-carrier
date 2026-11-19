// Audit Logs → "Contract, policy or file" filter (task 49.1, backend
// audit_record_filter.py): who did what to ONE record. The value is
// "contract:12", "policy:P-1001" or "file:march" — "" for every record.
import { useEffect, useState } from "react";
import { getRecordChoices } from "../api/changeHistory";
import { useDebouncedValue } from "../hooks/useDebouncedValue";

const KINDS = [["", "Any record"], ["contract", "A contract"], ["policy", "A policy"], ["file", "A file"]] as const;

export function AuditRecordFilter({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [kind, setKind] = useState(value.split(":")[0] || "");
  const [text, setText] = useState(value.includes(":") ? value.slice(value.indexOf(":") + 1) : "");
  const [contracts, setContracts] = useState<{ id: number; name: string }[] | null>(null);
  const typed = useDebouncedValue(text, 400);

  // Cleared from outside ("Clear filters").
  useEffect(() => { if (!value) { setKind(""); setText(""); } }, [value]);
  useEffect(() => {
    if (kind === "contract" && contracts === null) getRecordChoices().then(r => setContracts(r.contracts)).catch(() => setContracts([]));
  }, [kind, contracts]);
  useEffect(() => {
    if (kind === "policy" || kind === "file") onChange(typed.trim() ? `${kind}:${typed.trim()}` : "");
  }, [typed]); // eslint-disable-line react-hooks/exhaustive-deps

  const pickKind = (k: string) => { setKind(k); setText(""); onChange(""); };
  return (
    <>
      <label className="fbar-field">
        <span className="sub">Contract, policy or file</span>
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
      {(kind === "policy" || kind === "file") && (
        <label className="fbar-field">
          <span className="sub">{kind === "policy" ? "Policy number" : "File name has"}</span>
          <span className="search">
            <input type="search" value={text} onChange={e => setText(e.target.value)}
              placeholder={kind === "policy" ? "e.g. P-1001" : "e.g. march"}
              aria-label={kind === "policy" ? "Policy number" : "File name"} />
          </span>
        </label>)}
    </>
  );
}
