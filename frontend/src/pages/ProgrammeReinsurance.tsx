/**
 * A programme's reinsurance — Phase 26.2, seen from the programme.
 *
 * Every treaty protecting this programme, each drawn the same way the treaty's
 * own record draws it: quota shares as a split, excess-of-loss as a tower.
 * They are shown side by side rather than merged into one tower, because a
 * quota share and an XOL on the same book do not stack — the XOL usually sits
 * on the retained share — and one merged picture would claim they do.
 *
 * A treaty raised on another programme can be attached here; one raised on
 * this programme protects it always and cannot be taken off.
 */
import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, Link2, Plus, ShieldCheck, Unlink, Upload } from "lucide-react";
import { InfoTip } from "../components/InfoTip";
import { fieldErrors } from "../api/contractRecord";
import {
  attachTreaty, detachTreaty, getProgrammeTreaties, type ProgrammeTreaties,
} from "../api/treaty";
import { TreatyTower } from "../components/treaty/TreatyTower";
import { UploadTreatyModal } from "../components/treaty/UploadTreatyModal";
import { fmtDate } from "../utils/date";

const LIFE: Record<string, string> = {
  draft: "b-mut", pending: "b-warn", agreed: "b-ok", signed: "b-ok",
  active: "b-ok", expired: "b-mut", terminated: "b-crit", superseded: "b-mut",
};

export default function ProgrammeReinsurance() {
  const { programId } = useParams();
  const pid = Number(programId);
  const [data, setData] = useState<ProgrammeTreaties | null>(null);
  const [pick, setPick] = useState("");
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    getProgrammeTreaties(pid).then(setData).catch(e => setErr(fieldErrors(e).message));
  }, [pid]);

  async function attach() {
    setBusy(true); setErr(""); setNote("");
    try {
      const name = data?.attachable.find(a => String(a.contract_id) === pick)?.name;
      setData(await attachTreaty(pid, Number(pick)));
      setPick("");
      setNote(`${name ?? "The treaty"} now protects this programme.`);
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  async function detach(contractId: number, name: string) {
    setBusy(true); setErr(""); setNote("");
    try {
      setData(await detachTreaty(pid, contractId));
      setNote(`${name} no longer protects this programme.`);
    } catch (e) {
      setErr(fieldErrors(e).message);
    } finally {
      setBusy(false);
    }
  }

  const live = (data?.treaties ?? []).filter(
    t => !["terminated", "superseded"].includes(t.lifecycle));
  const past = (data?.treaties ?? []).filter(
    t => ["terminated", "superseded"].includes(t.lifecycle));

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>
              Reinsurance{data ? ` · ${data.programme.name}` : ""}
              <InfoTip text={"The treaties that protect this programme. A treaty "
                + "raised on another programme can be attached here too — a quota "
                + "share across the whole book usually protects several."} />
            </h2>
            <p>
              {data ? `${data.live_count} treat${data.live_count === 1 ? "y" : "ies"} `
                + "in place" : "Loading…"}
            </p>
          </div>
          <div className="actions">
            <Link to="/programs" className="btn"><ArrowLeft size={14} /> Programmes</Link>
            <button className="btn" type="button" onClick={() => setUploading(true)}>
              <Upload size={14} /> Upload signed treaty
            </button>
            <Link className="btn pri"
                  to={`/contracts/new?program_id=${pid}&type=insurer_reinsurer`}>
              <Plus size={14} /> Raise a new treaty
            </Link>
          </div>
        </div>

        {note && <div className="note ok" style={{ marginBottom: 14 }}>{note}</div>}
        {err && <div className="note warn" style={{ marginBottom: 14 }}>{err}</div>}

        {data && data.attachable.length > 0 && (
          <div className="card pad" style={{ marginBottom: 18, display: "flex",
               gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
            <div className="field" style={{ marginBottom: 0, flex: "1 1 320px" }}>
              <label>Attach a treaty raised on another programme</label>
              <select value={pick} onChange={e => setPick(e.target.value)}>
                <option value="">Choose a treaty…</option>
                {data.attachable.map(a => (
                  <option key={a.contract_id} value={a.contract_id}>
                    {a.name}{a.type_label ? ` — ${a.type_label}` : ""} ({a.lifecycle})
                  </option>
                ))}
              </select>
            </div>
            <button className="btn" type="button" disabled={!pick || busy} onClick={attach}>
              <Link2 size={14} /> Attach
            </button>
          </div>
        )}

        {data && live.length === 0 && (
          <div className="card">
            <div className="empty">
              No treaty protects this programme yet.{" "}
              <Link className="linkish"
                    to={`/contracts/new?program_id=${pid}&type=insurer_reinsurer`}>
                Raise one
              </Link>
              {data.attachable.length > 0 && " or attach one above"}.
            </div>
          </div>
        )}

        <div className="grid g-2" style={{ alignItems: "start" }}>
          {live.map(t => (
            <div className="card" key={t.contract_id}>
              <div className="card-h">
                <ShieldCheck size={16} className="ci" />
                <h3>
                  <Link className="linkish" to={`/contracts/${t.contract_id}`}>{t.name}</Link>
                </h3>
                <span className={`badge ${LIFE[t.lifecycle] ?? "b-mut"}`}>
                  <span className="d" />{t.lifecycle.replace(/_/g, " ")}
                </span>
                <span className="right">
                  {!t.is_home && (
                    <button className="btn sm ghost" type="button" disabled={busy}
                            title="Stop this treaty protecting this programme"
                            onClick={() => detach(t.contract_id, t.name)}>
                      <Unlink size={12} /> Take off
                    </button>
                  )}
                </span>
              </div>
              <div style={{ padding: "14px 20px" }}>
                <div style={{ fontWeight: 700, marginBottom: 4 }}>
                  {t.summary?.headline ?? "Structure not set up yet"}
                </div>
                <div className="sub" style={{ marginBottom: 12 }}>
                  {[t.summary?.type_label, t.lead_reinsurer && `led by ${t.lead_reinsurer}`,
                    `${fmtDate(t.inception_dt)} → ${fmtDate(t.expiry_dt)}`,
                    t.is_home ? "raised on this programme" : "attached"]
                    .filter(Boolean).join(" · ")}
                </div>
                {t.summary ? <TreatyTower summary={t.summary} compact /> : (
                  <Link className="linkish" to={`/contracts/${t.contract_id}`}>
                    Set out its structure →
                  </Link>
                )}
              </div>
            </div>
          ))}
        </div>

        <UploadTreatyModal open={uploading} programId={pid}
          onClose={() => setUploading(false)} />

        {past.length > 0 && (
          <div className="card" style={{ marginTop: 18 }}>
            <div className="card-h"><h3>Ended</h3></div>
            <div className="tbl-wrap">
              <table>
                <tbody>
                  {past.map(t => (
                    <tr key={t.contract_id}>
                      <td><Link className="linkish" to={`/contracts/${t.contract_id}`}>{t.name}</Link></td>
                      <td className="sub">{t.summary?.headline ?? "—"}</td>
                      <td><span className="badge b-mut"><span className="d" />{t.lifecycle}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
