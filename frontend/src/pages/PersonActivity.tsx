import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  getPersonDecisions, getPersonFiles,
  type PersonDecision, type PersonFile, type PersonFiles,
} from "../api/broker";
import { InfoTip } from "../components/InfoTip";
import { Pagination } from "../components/Pagination";
import { fmtDateTime } from "../utils/date";

const PAGE_SIZE = 25;
const DAYS = 30;

const KIND_LABEL: Record<string, string> = {
  fix: "Fixed", approve: "Approved", dismiss: "Dismissed", reject: "Rejected",
};

/** Where a decision → what changed, in words rather than raw column names. */
function describe(d: PersonDecision) {
  const where = [d.sheet, d.row != null ? `row ${d.row}` : null, d.field]
    .filter(Boolean).join(" · ");
  if (d.old_value != null && d.new_value != null && d.old_value !== d.new_value) {
    return `${where}: “${d.old_value}” → “${d.new_value}”`;
  }
  if (d.new_value != null) return `${where}: set to “${d.new_value}”`;
  return where || "—";
}

/**
 * Everything one person did: the files they SENT, split by programme, and the
 * exceptions they put right.
 *
 * Files come first, because that is what a broker admin acts on. "564 open" on
 * the dashboard is four files across two programmes — and a programme is the
 * unit being reviewed, since the contract, the rules and the carrier all hang
 * off it. Each file carries its own counts and its own way into Exception
 * Triage, so the admin chooses which one to open instead of being dropped into
 * whichever file happened to be last.
 *
 * The table below is the receipt behind those files' "Resolved" column: every
 * decision made on them, with the person who made it named. Anyone at the
 * broker can clear the queue on anyone's file, so the name is the part that
 * cannot be read anywhere else.
 */
export default function PersonActivity() {
  const { userId } = useParams();
  const nav = useNavigate();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Awaited<ReturnType<typeof getPersonDecisions>> | null>(null);
  const [err, setErr] = useState(false);
  const [files, setFiles] = useState<PersonFiles | null>(null);
  const [filesErr, setFilesErr] = useState(false);

  useEffect(() => {
    if (!userId) return;
    setErr(false);
    getPersonDecisions(Number(userId), { days: DAYS, page, pageSize: PAGE_SIZE })
      .then(setData).catch(() => setErr(true));
  }, [userId, page]);

  // Their own pass: the file list is the whole window, not a page of decisions.
  useEffect(() => {
    if (!userId) return;
    setFilesErr(false);
    getPersonFiles(Number(userId), { days: DAYS, pageSize: 100 })
      .then(setFiles).catch(() => setFilesErr(true));
  }, [userId]);

  const reviewPath = (d: PersonDecision) =>
    `/uploads/${d.export_id}/exceptions?download=${d.export_id}&from=broker`;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              {data?.person.name ?? files?.person.name ?? "Team member"}
              <InfoTip text={`What they sent and what they put right, in the last ${DAYS} days.`} />
            </h2>
          </div>
          <div className="actions">
            <button className="btn" type="button" onClick={() => nav("/broker/team-activity")}>
              ← Back to Team Activity
            </button>
          </div>
        </div>

        <FilesSection files={files} err={filesErr} />

        {/* The receipt behind the "Resolved" column above: the same work, one
            row per decision, with the person who made it named. Anyone at this
            broker can clear the queue on anyone's file — the admin cleared 65
            of Cleap's — so "who" is the part an admin cannot read anywhere
            else. */}
        <div className="card" style={{ marginTop: 24 }}>
          <div className="card-h">
            <h3>Exceptions they resolved by brokers</h3>
          </div>
          {err ? (
            <div className="empty">Could not load this person's activity.</div>
          ) : !data ? (
            <div className="empty">Loading…</div>
          ) : data.total === 0 ? (
            <div className="empty">Nothing has been resolved on their files in this period.</div>
          ) : (
            <>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Resolved by</th>
                      <th>File</th><th>Policy</th><th>What changed</th>
                      <th>Kind</th><th>When</th><th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map(d => (
                      <tr key={d.id}>
                        <td><b>{d.decided_by ?? "—"}</b></td>
                        <td>
                          {d.filename ?? "—"}
                          {d.programme && <div className="muted" style={{ fontSize: 12 }}>{d.programme}</div>}
                        </td>
                        <td>{d.policy_number ?? "—"}</td>
                        <td className="muted">{describe(d)}</td>
                        <td>{KIND_LABEL[d.kind] ?? d.kind}</td>
                        <td className="muted">{fmtDateTime(d.decided_at)}</td>
                        <td style={{ textAlign: "right" }}>
                          {d.export_id != null && (
                            <Link className="btn sm" to={reviewPath(d)}>Open file →</Link>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={page} pageSize={PAGE_SIZE} totalItems={data.total}
                          pageCount={Math.max(1, Math.ceil(data.total / PAGE_SIZE))}
                          onPageChange={setPage} noun="decisions" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** The files this person sent, grouped by programme.
 *
 *  Programme first, then the files under it: a broker admin reviews one
 *  programme's book at a time, and two files on the same programme are the
 *  same contract and the same rules. Each file opens its own Exception Triage
 *  screen, and the counts here are the ones that screen shows, because both
 *  come from the same tally on the server. */
function FilesSection({ files, err }: { files: PersonFiles | null; err: boolean }) {
  if (err) return <div className="card"><div className="empty">Could not load their files.</div></div>;
  if (!files) return <div className="card"><div className="empty">Loading…</div></div>;
  if (!files.total) {
    return (
      <div className="card">
        <div className="card-h"><h3>Files they sent</h3></div>
        <div className="empty">
          They have not sent a file in this period. A file a carrier ran for your
          broker is not counted here — it belongs to no one person.
        </div>
      </div>
    );
  }
  const t = files.totals;
  const byProg = new Map<string, PersonFile[]>();
  for (const f of files.items) {
    const k = String(f.programme_id ?? "none");
    if (!byProg.has(k)) byProg.set(k, []);
    byProg.get(k)!.push(f);
  }
  return (
    <div className="card">
      <div className="card-h">
        <h3>Files they sent</h3>
        <span className="muted" style={{ fontSize: 13 }}>
          {t.files} {t.files === 1 ? "file" : "files"} · {t.rows} rows ·{" "}
          <b style={{ color: "var(--p-ink)" }}>{t.open}</b> of {t.exceptions} exceptions open
        </span>
      </div>

      {files.by_programme.map(g => {
        const rows = byProg.get(String(g.id ?? "none")) ?? [];
        return (
          <div key={String(g.id ?? "none")} style={{ padding: "4px 20px 16px" }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap",
                          margin: "12px 0 8px" }}>
              <b style={{ fontSize: 14 }}>{g.name}</b>
              {g.carrier && <span className="muted" style={{ fontSize: 12 }}>{g.carrier}</span>}
              <span style={{ marginLeft: "auto", fontSize: 12.5, color: "var(--p-muted)",
                             fontVariantNumeric: "tabular-nums" }}>
                {g.files} {g.files === 1 ? "file" : "files"} · {g.rows} rows ·{" "}
                <b style={{ color: g.open ? "var(--p-ink)" : "var(--p-faint)" }}>{g.open}</b> open
                {g.put_right > 0 && <> · {g.put_right} put right</>}
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
                    <th style={{ textAlign: "right" }} title="By anyone on your team">
                      Resolved
                    </th>
                    <th>Sent</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(f => (
                    <tr key={f.export_id}>
                      <td><b>{f.filename ?? `Export ${f.export_id}`}</b></td>
                      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{f.rows}</td>
                      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                                   color: f.exceptions ? undefined : "var(--p-faint)" }}>
                        {f.exceptions}
                      </td>
                      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                                   color: f.open ? undefined : "var(--p-faint)" }}>
                        {f.open}
                      </td>
                      <td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums",
                                   color: f.put_right ? undefined : "var(--p-faint)" }}>
                        {f.put_right}
                      </td>
                      <td className="muted">{fmtDateTime(f.created_at)}</td>
                      <td style={{ textAlign: "right" }}>
                        <Link className="btn sm"
                              to={`/uploads/${f.source_upload_id ?? f.export_id}/exceptions`
                                  + `?download=${f.export_id}&from=broker`}>
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
      {files.total > files.items.length && (
        <div className="muted" style={{ padding: "0 20px 16px", fontSize: 12.5 }}>
          Showing the {files.items.length} most recent of {files.total} files.
        </div>
      )}
    </div>
  );
}
