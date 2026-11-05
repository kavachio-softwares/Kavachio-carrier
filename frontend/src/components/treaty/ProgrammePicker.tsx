/**
 * "Programmes that feed it" on a reinsurance bordereau setup: one row per
 * programme this treaty protects, ticked when its bordereaux make up the
 * statements.
 *
 * This treaty is named once, above the table — not repeated in every row.
 * Each row shows only what tells programmes apart: their broker contracts
 * (folded to a count, opened on demand) and the OTHER treaties that also
 * protect them, which take their own statements from the same bordereaux.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { ChevronDown, ChevronRight, ShieldCheck } from "lucide-react";
import type { SetupRecord } from "../../api/ceded";
import { InfoTip } from "../InfoTip";

type Opts = SetupRecord["options"];

const cap = (s: string | null) => (s ? s[0].toUpperCase() + s.slice(1) : "");

export function ProgrammePicker({ options, value, onChange }: {
  options: Opts;
  value: number[];
  onChange: (ids: number[]) => void;
}) {
  const [open, setOpen] = useState<Record<number, boolean>>({});
  const progs = options.programmes;
  const treaties = options.treaties ?? [];
  const contracts = options.contracts ?? [];
  const me = treaties.find(t => t.this);
  const chosen = progs.filter(p => value.includes(p.id));
  const fedBy = contracts.filter(k => k.program_id != null && value.includes(k.program_id));
  const all = progs.length > 0 && chosen.length === progs.length;

  const toggle = (id: number, on: boolean) =>
    onChange(on ? [...value, id] : value.filter(x => x !== id));

  return (
    <div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <span className="sub">
          <b>{chosen.length}</b> of {progs.length} programme{progs.length === 1 ? "" : "s"} chosen
          {" · "}<b>{fedBy.length}</b> broker contract{fedBy.length === 1 ? "" : "s"} feed
          {fedBy.length === 1 ? "s" : ""} {me ? <b>{me.name}</b> : "this treaty"}
        </span>
      </div>
      <div className="tbl-wrap" style={{ border: "1px solid var(--p-border)", borderRadius: 10 }}>
        <table>
          <thead>
            <tr>
              <th style={{ width: 36 }}>
                <input type="checkbox" checked={all} aria-label="Choose every programme"
                       onChange={e => onChange(e.target.checked ? progs.map(p => p.id) : [])} />
              </th>
              <th className="l">Programme</th>
              <th className="l">Broker contracts</th>
              <th className="l">
                Other treaties on it
                <InfoTip text="These treaties protect the same programme. Each makes its own statements from the same bordereaux, on its own setup — choosing a programme here does not affect them." />
              </th>
            </tr>
          </thead>
          <tbody>
            {progs.map(p => {
              const on = value.includes(p.id);
              const ks = contracts.filter(k => k.program_id === p.id);
              const others = treaties.filter(t => !t.this && t.program_ids.includes(p.id));
              const brokers = [...new Set(ks.map(k => k.broker).filter(Boolean))] as string[];
              const shown = !!open[p.id];
              return (
                <tr key={p.id} style={{ verticalAlign: "top",
                    background: on ? "var(--p-ok-bg, #f0fdf9)" : undefined }}>
                  <td>
                    <input type="checkbox" checked={on} aria-label={`Choose ${p.name}`}
                           onChange={e => toggle(p.id, e.target.checked)} />
                  </td>
                  <td className="l">
                    <label style={{ cursor: "pointer", fontWeight: 600 }}
                           onClick={() => toggle(p.id, !on)}>{p.name}</label>
                    {p.is_home && (
                      <span className="badge b-mut" style={{ fontSize: 10, marginLeft: 6 }}>Home programme</span>
                    )}
                  </td>
                  <td className="l">
                    {ks.length === 0 ? <span className="sub">None</span> : (<>
                      <button type="button" className="linkish"
                              style={{ display: "inline-flex", alignItems: "center", gap: 2,
                                       background: "none", border: 0, padding: 0, cursor: "pointer" }}
                              onClick={() => setOpen(o => ({ ...o, [p.id]: !shown }))}>
                        {shown ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                        {ks.length} contract{ks.length === 1 ? "" : "s"}
                      </button>
                      {brokers.length > 0 && (
                        <div className="sub">{brokers.slice(0, 3).join(", ")}
                          {brokers.length > 3 ? ` +${brokers.length - 3} more` : ""}</div>
                      )}
                      {shown && (
                        <div style={{ marginTop: 6, display: "grid", gap: 3 }}>
                          {ks.map(k => (
                            <div key={k.id}>
                              <Link className="linkish" to={`/contracts/${k.id}`}>{k.name}</Link>
                              <span className="sub"> · {[k.broker, cap(k.status)].filter(Boolean).join(" · ")}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </>)}
                  </td>
                  <td className="l">
                    {others.length === 0 ? <span className="sub">Only this treaty</span> : (
                      <div style={{ display: "grid", gap: 3 }}>
                        {others.map(t => (
                          <div key={t.id} style={{ display: "flex", gap: 5, alignItems: "baseline", flexWrap: "wrap" }}>
                            <ShieldCheck size={12} style={{ alignSelf: "center", flexShrink: 0 }} />
                            <Link className="linkish" to={`/contracts/${t.id}`}>{t.name}</Link>
                            <span className="sub">{[t.type_label, cap(t.status)].filter(Boolean).join(" · ")}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
