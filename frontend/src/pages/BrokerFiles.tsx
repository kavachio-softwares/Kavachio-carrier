import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { currentMga } from "../auth";
import { fmtDateTime } from "../utils/date";

const DAYS = 30;

type File = {
  export_id: number; source_upload_id: number | null; filename: string | null;
  programme_id: number | null; programme: string | null;
  status: string | null; created_at: string | null;
  rows: number; rows_flagged: number;
  exceptions: number; open: number; put_right: number;
};

type Programme = {
  id: number | null; name: string;
  files: number; rows: number; exceptions: number; open: number; put_right: number;
};

type Resp = {
  broker: { id: number; name: string | null };
  items: File[]; total: number;
  by_programme: Programme[];
  totals: { files: number; rows: number; exceptions: number; open: number; put_right: number };
};

/**
 * One broker's files for this carrier, split by programme.
 *
 * The Broker Performance bar is one number for a company — "457 of 522 open" —
 * and a carrier cannot act on that. Those files sit on different programmes,
 * and a programme is the unit being reviewed: the contract, the rules and the
 * bordereau setup all hang off it. So the work is grouped the way it will be
 * dealt with, and every file opens its own Exception Triage screen.
 *
 * Same shape as the broker's own view of its team (PersonActivity) — the two
 * sides read the same numbers about the same runs, from opposite ends.
 */
export default function BrokerFiles() {
  const { brokerId } = useParams();
  const nav = useNavigate();
  const mga = currentMga();
  const [data, setData] = useState<Resp | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    if (!brokerId) return;
    setErr(false);
    api.get<Resp>(`/dashboard/brokers/${brokerId}/files`,
                  { params: { mga, days: DAYS, page_size: 100 } })
      .then(r => setData(r.data)).catch(() => setErr(true));
  }, [brokerId, mga]);

  const t = data?.totals;
  const byProg = new Map<string, File[]>();
  for (const f of data?.items ?? []) {
    const k = String(f.programme_id ?? "none");
    if (!byProg.has(k)) byProg.set(k, []);
    byProg.get(k)!.push(f);
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>{data?.broker.name ?? "Broker"}</h2>
            <p>
              What they sent you in the last {DAYS} days, and what is still open —
              by programme.
            </p>
          </div>
          <div className="actions">
            <button className="btn" type="button" onClick={() => nav(-1)}>← Back</button>
            <Link className="btn" to={`/brokers/${brokerId}`}>Broker record</Link>
          </div>
        </div>

        {err ? (
          <div className="card"><div className="empty">Could not load this broker's files.</div></div>
        ) : !data ? (
          <div className="card"><div className="empty">Loading…</div></div>
        ) : !data.total ? (
          <div className="card">
            <div className="empty">
              They have not sent a file in the last {DAYS} days.
            </div>
          </div>
        ) : (
          <div className="card">
            <div className="card-h">
              <h3>Files they sent</h3>
              <span className="muted" style={{ fontSize: 13 }}>
                {t!.files} {t!.files === 1 ? "file" : "files"} · {t!.rows} rows ·{" "}
                <b style={{ color: "var(--p-ink)" }}>{t!.open}</b> of {t!.exceptions} exceptions open
              </span>
            </div>

            {data.by_programme.map(g => {
              const rows = byProg.get(String(g.id ?? "none")) ?? [];
              return (
                <div key={String(g.id ?? "none")} style={{ padding: "4px 20px 16px" }}>
                  <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap",
                                margin: "12px 0 8px" }}>
                    {g.id ? (
                      <Link to={`/programs/${g.id}/setup`} style={{ fontSize: 14, fontWeight: 700 }}>
                        {g.name}
                      </Link>
                    ) : <b style={{ fontSize: 14 }}>{g.name}</b>}
                    <span style={{ marginLeft: "auto", fontSize: 12.5, color: "var(--p-muted)",
                                   fontVariantNumeric: "tabular-nums" }}>
                      {g.files} {g.files === 1 ? "file" : "files"} · {g.rows} rows ·{" "}
                      <b style={{ color: g.open ? "var(--p-ink)" : "var(--p-faint)" }}>{g.open}</b> open
                      {g.put_right > 0 && <> · {g.put_right} resolved</>}
                    </span>
                  </div>
                  <div className="tbl-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>File</th>
                          <th style={{ textAlign: "right" }}>Rows</th>
                          <th style={{ textAlign: "right" }}>Exceptions</th>
                          <th style={{ textAlign: "right" }}>Still open</th>
                          <th style={{ textAlign: "right" }}>Resolved</th>
                          <th>Sent</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map(f => (
                          <tr key={f.export_id}>
                            <td><b>{f.filename ?? `Export ${f.export_id}`}</b></td>
                            <Num v={f.rows} />
                            <Num v={f.exceptions} />
                            <Num v={f.open} />
                            <Num v={f.put_right} />
                            <td className="muted">{fmtDateTime(f.created_at)}</td>
                            <td style={{ textAlign: "right" }}>
                              <Link className="btn sm"
                                    to={`/uploads/${f.source_upload_id ?? f.export_id}/exceptions`
                                        + `?download=${f.export_id}&from=home`}>
                                {f.open ? "Review →" : "Open →"}
                              </Link>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              );
            })}

            {data.total > data.items.length && (
              <div className="muted" style={{ padding: "0 20px 16px", fontSize: 12.5 }}>
                Showing the {data.items.length} most recent of {data.total} files.
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** A count, right-aligned, greyed when it is zero — a zero is an answer, not a
 *  gap, and it should not read as loudly as a real number. */
function Num({ v }: { v: number }) {
  return (
    <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                 color: v ? undefined : "var(--p-faint)" }}>{v}</td>
  );
}
