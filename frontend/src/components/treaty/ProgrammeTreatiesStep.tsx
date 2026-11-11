/**
 * Configure Program — a programme's reinsurance: each treaty protecting it,
 * with its structure and its reinsurance bordereau setup, and the ways to add
 * one — attach a treaty raised on another programme, upload a signed one,
 * raise a new one. A treaty raised on this programme protects it always; one
 * attached can be taken off.
 *
 * A treaty is the carrier's own, irrespective of the brokers: none of it waits
 * on a broker, their contract or their setup. A setup with no broker setup to
 * read from takes the carrier's own input template instead.
 *
 * `part` shows only the treaties ("treaties") or only their setups ("setups");
 * without it, both. Optional, all of it: a programme takes files without one.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Link2, Plus, ShieldCheck, Unlink, Upload } from "lucide-react";
import { fieldErrors } from "../../api/contractRecord";
import { showDone, showError } from "../ResultPopup";
import {
  attachTreaty, detachTreaty, getProgrammeTreaties, type ProgrammeTreaties,
} from "../../api/treaty";
import { listSetups, type SetupListItem } from "../../api/ceded";
import { UploadTreatyModal } from "./UploadTreatyModal";
import { fmtDate } from "../../utils/date";

const ENDED = ["terminated", "superseded"];

const SETUP_LINE: Record<string, { text: string; cls: string }> = {
  active: { text: "Reinsurance bordereau in use — statements are prepared every period", cls: "text-ok" },
  pending_approval: { text: "Reinsurance bordereau setup waiting for approval", cls: "text-warn" },
  draft: { text: "Reinsurance bordereau setup started, not finished yet", cls: "text-warn" },
};

export default function ProgrammeTreatiesStep({ programId, part, onChange, onAddTreaty }: {
  programId: number;
  part?: "treaties" | "setups";
  /** Setups only, with no treaty: to where one is added. */
  onAddTreaty?: () => void;
  /** Live treaties and how many have a setup in use — for the stepper. */
  onChange?: (live: number, inUse: number) => void;
}) {
  const [data, setData] = useState<ProgrammeTreaties | null>(null);
  const [setups, setSetups] = useState<Record<number, SetupListItem>>({});
  const [pick, setPick] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);

  useEffect(() => {
    getProgrammeTreaties(programId).then(setData).catch(e => setErr(fieldErrors(e).message));
    listSetups().then(items => setSetups(Object.fromEntries(items.map(i => [i.contract_id, i]))))
      .catch(() => setSetups({}));
  }, [programId]);

  const live = (data?.treaties ?? []).filter(t => !ENDED.includes(t.lifecycle));
  const inUse = live.filter(t => setups[t.contract_id]?.status === "active").length;
  useEffect(() => { if (data) onChange?.(live.length, inUse); },
    [data, live.length, inUse]); // eslint-disable-line react-hooks/exhaustive-deps

  async function run(fn: () => Promise<ProgrammeTreaties>, ok: string) {
    setBusy(true);
    try {
      setData(await fn());
      showDone(ok);
    } catch (e) {
      showError(e);
    } finally {
      setBusy(false);
    }
  }

  const raiseUrl = `/contracts/new?program_id=${programId}&type=insurer_reinsurer`;
  const showTreaties = part !== "setups";
  const showSetups = part !== "treaties";

  return (
    <div>
      <p className="mb-3 text-xs leading-relaxed text-ink-muted">
        {part === "treaties"
          ? "The treaties that protect this programme — its reinsurance. Optional: a programme takes files without one."
          : part === "setups"
          ? "Each treaty's reinsurance bordereau — made every period, one statement per reinsurer."
          : "The treaties that protect this programme and each one's reinsurance bordereau — one statement per reinsurer every period. "
            + "Irrespective of the brokers: set them up at any time. Optional: a programme takes files without one."}
      </p>
      {err && <div className="mb-3 rounded border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">{err}</div>}

      {!data ? (
        <div className="text-sm text-ink-muted">Loading…</div>
      ) : (
        <div className="space-y-1.5">
          {live.length === 0 && (
            <div className="rounded border border-dashed border-border px-3 py-4 text-center text-sm text-ink-muted">
              {showTreaties
                ? "No treaty protects this programme yet — raise one, upload a signed one, or attach one raised on another programme."
                : <>No treaty protects this programme, so there is no reinsurance bordereau to set up.{" "}
                    {onAddTreaty && <button type="button" onClick={onAddTreaty}
                      className="font-semibold text-navy hover:underline">Add a treaty</button>}</>}
            </div>
          )}
          {live.map(t => {
            const su = setups[t.contract_id];
            const line = su?.status ? SETUP_LINE[su.status] : null;
            return (
              <div key={t.contract_id}
                className="flex flex-wrap items-center gap-3 rounded border border-border px-3 py-2.5">
                <ShieldCheck size={16} className="shrink-0 text-navy" />
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2 text-sm font-medium">
                    <Link to={`/contracts/${t.contract_id}`} className="text-ink hover:underline">{t.name}</Link>
                    <span className="rounded-full bg-surface-2 px-2 py-0.5 text-[10.5px] font-medium capitalize text-ink-muted">
                      {t.lifecycle.replace(/_/g, " ")}
                    </span>
                    <span className="text-[11px] font-normal text-ink-soft">
                      {t.is_home ? "Raised on this programme" : "Attached"}
                    </span>
                  </span>
                  <span className="block text-xs text-ink-muted">
                    {[t.summary?.headline ?? "Structure not set up yet",
                      t.lead_reinsurer && `led by ${t.lead_reinsurer}`,
                      (t.inception_dt || t.expiry_dt) && `${fmtDate(t.inception_dt)} → ${fmtDate(t.expiry_dt)}`]
                      .filter(Boolean).join(" · ")}
                  </span>
                  {showSetups && (
                    <span className={`block text-xs ${line?.cls ?? "text-warn"}`}>
                      {line?.text ?? (!t.summary ? "Set out its structure, then set up its reinsurance bordereau"
                        : su && su.has_wording === false ? "Needs its signed wording before its reinsurance bordereau can be set up"
                        : "No reinsurance bordereau setup yet")}
                    </span>
                  )}
                </span>
                <div className="flex shrink-0 items-center gap-2">
                  {!showSetups ? (
                    !t.summary && (
                      <Link to={`/contracts/${t.contract_id}?tab=treaty`}
                        className="inline-flex items-center rounded border border-border px-3 py-1.5 text-xs font-medium text-ink hover:bg-surface-2 hover:no-underline">
                        Set out structure
                      </Link>
                    )
                  ) : !t.summary ? (
                    <Link to={`/contracts/${t.contract_id}?tab=treaty`}
                      className="inline-flex items-center rounded border border-border px-3 py-1.5 text-xs font-medium text-ink hover:bg-surface-2 hover:no-underline">
                      Set out structure
                    </Link>
                  ) : su?.status === "active" || su?.status === "pending_approval" ? (
                    <Link to={`/reinsurance/setups/${t.contract_id}`}
                      className="inline-flex items-center rounded border border-border px-3 py-1.5 text-xs font-medium text-ink hover:bg-surface-2 hover:no-underline">
                      View setup
                    </Link>
                  ) : (
                    <Link to={`/reinsurance/setups/${t.contract_id}/edit`}
                      className="inline-flex items-center rounded bg-navy px-3 py-1.5 text-xs font-medium text-white hover:bg-navy-dark hover:no-underline">
                      {su?.status === "draft" ? "Finish the setup" : "Reinsurance Bordereau Setup"}
                    </Link>
                  )}
                  {showTreaties && !t.is_home && (
                    <button type="button" disabled={busy}
                      title="Stop this treaty protecting this programme"
                      onClick={() => run(() => detachTreaty(programId, t.contract_id),
                        `${t.name} no longer protects this programme.`)}
                      className="inline-flex items-center gap-1 rounded border border-border px-2.5 py-1.5 text-xs hover:bg-surface-2 disabled:opacity-50">
                      <Unlink size={12} /> Take off
                    </button>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {data && showTreaties && (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          {data.attachable.length > 0 && (<>
            <label className="min-w-[260px] flex-1 text-xs text-ink-muted">
              Attach a treaty raised on another programme
              <select value={pick} onChange={e => setPick(e.target.value)}
                className="mt-1 block w-full rounded border border-border bg-white px-2 py-1.5 text-sm text-ink">
                <option value="">Choose a treaty…</option>
                {data.attachable.map(a => (
                  <option key={a.contract_id} value={a.contract_id}>
                    {a.name}{a.type_label ? ` — ${a.type_label}` : ""} ({a.lifecycle})
                  </option>
                ))}
              </select>
            </label>
            <button type="button" disabled={!pick || busy}
              onClick={() => {
                const name = data.attachable.find(a => String(a.contract_id) === pick)?.name ?? "The treaty";
                run(() => attachTreaty(programId, Number(pick)), `${name} now protects this programme.`)
                  .then(() => setPick(""));
              }}
              className="inline-flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-xs font-medium hover:bg-surface-2 disabled:opacity-50">
              <Link2 size={13} /> Attach
            </button>
          </>)}
          <span className="ml-auto flex gap-2">
            <button type="button" onClick={() => setUploading(true)}
              className="inline-flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-xs font-medium hover:bg-surface-2">
              <Upload size={13} /> Upload a treaty
            </button>
            <Link to={raiseUrl}
              className="inline-flex items-center gap-1.5 rounded bg-navy px-3 py-1.5 text-xs font-medium text-white hover:bg-navy-dark hover:no-underline">
              <Plus size={13} /> Raise a new treaty
            </Link>
          </span>
        </div>
      )}

      <UploadTreatyModal open={uploading} programId={programId} onClose={() => setUploading(false)} />
    </div>
  );
}
