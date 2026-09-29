import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getBrokerExceptions, type BrokerExceptionFile } from "../api/broker";
import { useBrokerCarrierId } from "../brokerCarrier";
import { Pagination } from "../components/Pagination";
import { InfoTip } from "../components/InfoTip";
import { fmtDateTime } from "../utils/date";

const PAGE_SIZE = 10;

/** The exception screen a file opens on — the same one Process Bordereau uses. */
const reviewPath = (f: BrokerExceptionFile) =>
  `/uploads/${f.export_id}/exceptions?download=${f.export_id}&from=broker`;

/** What the dashboard's Exceptions to Review tile counts, file by file.
 *
 *  The tile's number is the sum of this page's "Open" column — both come from
 *  GET /broker/exceptions' tally — so clicking through never lands on a
 *  different total. Most still-open first: the file with the most left to do
 *  is the one to open. */
export default function BrokerExceptions() {
  const carrierId = useBrokerCarrierId();
  const [data, setData] = useState<Awaited<ReturnType<typeof getBrokerExceptions>> | null>(null);
  const [err, setErr] = useState(false);
  const [page, setPage] = useState(1);

  useEffect(() => {
    setData(null); setErr(false); setPage(1);
    getBrokerExceptions(carrierId).then(setData).catch(() => setErr(true));
  }, [carrierId]);

  const items = data?.items ?? [];
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const pageNow = Math.min(page, pageCount);
  const shown = items.slice((pageNow - 1) * PAGE_SIZE, pageNow * PAGE_SIZE);
  const showCarrier = carrierId == null;
  const t = data?.totals;

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>Exceptions to Review
              <InfoTip text={"Every file you have processed that still carries exceptions, "
                + "the one with the most left open first. A file you fixed and re-ran "
                + "shows once, as it is now."} />
            </h2>
          </div>
        </div>

        <div className="tiles" style={{ marginBottom: 18 }}>
          <div className={`tile${(t?.open ?? 0) > 0 ? " alert" : ""}`}>
            <div className="k">Still open</div>
            <div className="v" style={{ color: "var(--p-crit)" }}>{t ? t.open : "—"}</div>
          </div>
          <div className="tile">
            <div className="k">Put right</div>
            <div className="v" style={{ color: "var(--p-ok)" }}>{t ? t.put_right : "—"}</div>
          </div>
          <div className="tile">
            <div className="k">Files with open exceptions</div>
            <div className="v">{t ? t.files_open : "—"}</div>
          </div>
        </div>

        <div className="card">
          {err ? (
            <div className="empty">Could not load your exceptions.</div>
          ) : !data ? (
            <div className="empty">Loading…</div>
          ) : items.length === 0 ? (
            <div className="empty">No exceptions on any of your files.</div>
          ) : (
            <>
              <div className="tbl-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>File Name</th>{showCarrier && <th>Carrier</th>}<th>Programme</th>
                      <th>Processed</th><th>Rows</th><th>Exceptions</th>
                      <th>Still open</th><th>Put right</th><th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {shown.map(f => (
                      <tr key={f.export_id}>
                        <td><b>{f.filename ?? `File ${f.export_id}`}</b></td>
                        {showCarrier && <td className="muted">{f.carrier ?? "—"}</td>}
                        <td>{f.programme ?? "—"}</td>
                        <td className="muted">{fmtDateTime(f.created_at)}</td>
                        <td>{f.rows}</td>
                        <td>{f.exceptions}</td>
                        <td>
                          {f.open > 0
                            ? <span className="badge b-crit"><span className="d" />{f.open}</span>
                            : <span className="badge b-ok"><span className="d" />0</span>}
                        </td>
                        <td>{f.put_right}</td>
                        <td style={{ whiteSpace: "nowrap" }}>
                          <Link className="linkish" to={reviewPath(f)}>Review →</Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <Pagination page={pageNow} pageCount={pageCount} pageSize={PAGE_SIZE}
                totalItems={items.length} onPageChange={setPage} noun="files" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
