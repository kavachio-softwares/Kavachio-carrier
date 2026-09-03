import { useEffect, useState } from "react";
import { useParams, useSearchParams, useNavigate } from "react-router-dom";
import {
  getUploadExceptions, getDownloadExceptions, type UploadExceptionsResponse,
} from "../api/validation";
import { groupByRule, tallyDecisions, type RuleGroup } from "../components/ExceptionCards";
import ExceptionDecisionTable from "../components/ExceptionDecisionTable";
import RuleExplanationBlock, { hasExplanation } from "../components/RuleExplanation";

const SEV_SPINE: Record<string, string> = { critical: "crit", warning: "warn", info: "info" };
const SEV_BADGE: Record<string, string> = { critical: "b-crit", warning: "b-warn", info: "b-info" };
const SEV_LABEL: Record<string, string> = { critical: "Critical", warning: "Warning", info: "Info" };

/** SCREEN B — Review table for one rule. route: /uploads/:uploadId/exceptions/rule/:ruleId */
export default function RuleReview() {
  const { uploadId = "", ruleId = "" } = useParams();
  const [params] = useSearchParams();
  const downloadId = params.get("download");   // output-stage (per-download) view
  const nav = useNavigate();
  const [data, setData] = useState<UploadExceptionsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // True only once the reviewer actually saves a decision during THIS visit.
  // The back-navigation "Fix & Validate" reminder is gated on this so it never
  // fires for a look-only visit to a rule whose decisions were saved earlier.
  const [savedThisSession, setSavedThisSession] = useState(false);

  async function load() {
    setErr(null);
    try {
      if (downloadId) {
        const exceptions = await getDownloadExceptions(downloadId);
        setData({
          success: true, uploadId, validated: true, run: null, exceptions,
          source_file: null, mga: null, mapper_id: null,
          has_source_blob: false, mapper_spec: null,
        });
      } else {
        setData(await getUploadExceptions(uploadId));
      }
    }
    catch (e: any) { setErr(e?.response?.data?.message ?? e?.message ?? "Failed to load."); }
  }
  useEffect(() => { load(); }, [uploadId, downloadId]);

  const groups = groupByRule(data?.exceptions ?? []);
  // A rule can head more than one card (its own violations, and rows it flagged
  // for a different reason — see RuleGroup.checkKind), so a bare rule-id URL is
  // ambiguous: it means the rule's OWN card, and the exact group key is what
  // addresses the others. Hence key first, then the rule's own card.
  const group: RuleGroup | undefined =
    groups.find(g => g.ruleKey === ruleId)
    ?? groups.find(g => String(g.ruleId) === ruleId && !g.checkKind)
    ?? groups.find(g => String(g.ruleId) === ruleId);

  // `from` is carried BACK as well as in. UploadExceptions puts it on the link
  // that reaches this screen; dropping it on the way home lost the context that
  // decides both the sidebar highlight and where its own back button goes —
  // which for a broker is a different Process Bordereau from the carrier's.
  const backLink = (() => {
    const qs = new URLSearchParams();
    if (downloadId) qs.set("download", String(downloadId));
    const from = params.get("from");
    if (from) qs.set("from", from);
    const q = qs.toString();
    return `/uploads/${uploadId}/exceptions${q ? `?${q}` : ""}`;
  })();

  // Summary of the decisions already recorded on this rule (from the saved
  // statuses). Handed to the Exceptions screen on the way back so it can remind
  // the reviewer that Approved/Fixed values still need Fix & Validate to apply.
  // undefined when nothing has been decided — then no prompt is shown.
  const backState = (() => {
    if (!group || !savedThisSession) return undefined;
    const c = tallyDecisions(group.items);
    if (c.approve + c.fix + c.dismiss === 0) return undefined;
    return {
      reviewed: {
        ruleName: group.ruleName,
        approve: c.approve, fix: c.fix, dismiss: c.dismiss, pending: c.pending,
      },
    };
  })();
  const goBack = () => nav(backLink, { state: backState });

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>{group ? group.ruleName : "Rule Review"}</h2>
            {group && (
              <p className="mono">
                {group.ruleId != null ? `RULE-${group.ruleId}` : group.ruleKey}
                {" · "}{SEV_LABEL[group.severity] ?? group.severity}
                {" · "}{group.count} affected {group.count === 1 ? "policy" : "policies"}
              </p>
            )}
          </div>
          <div className="actions">
            <button className="btn" onClick={goBack}>← All Exceptions</button>
          </div>
        </div>

        {err ? (
          <div className="note warn">{err}</div>
        ) : !group ? (
          <div className="card pad">
            <p className="muted" style={{ fontSize: 13, margin: 0 }}>
              No exceptions for this rule.{" "}
              <span className="linkish" onClick={goBack}>Back To Exceptions</span>
            </p>
          </div>
        ) : (
          <>
            {/* rule header + contract clause */}
            <div className={`card spine ${SEV_SPINE[group.severity] ?? "info"} pad`} style={{ marginBottom: 18 }}>
              <div style={{
                display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
                // The block below now also renders for an explanation, so the gap
                // has to account for it — otherwise a rule with no error_message
                // and no clause butts its explanation against the severity badge.
                marginBottom: (group.errorMessage || group.contractClause
                               || hasExplanation(group.explanation)) ? 10 : 0,
              }}>
                <span className={`badge ${SEV_BADGE[group.severity] ?? "b-info"}`}>
                  <span className="d" />{SEV_LABEL[group.severity] ?? group.severity}
                </span>
                {group.fieldPath && <span className="tag-pill">{group.fieldPath}</span>}
                <span className="muted" style={{ marginLeft: "auto", fontSize: 12.5 }}>
                  {group.count} {group.count === 1 ? "policy" : "policies"} affected
                </span>
              </div>
              {/* What the rule requires, why these rows failed and what to do —
                  the contract wording sits behind "Where this comes from". */}
              {hasExplanation(group.explanation) || group.contractClause ? (
                <RuleExplanationBlock
                  explanation={group.explanation}
                  clauseFallback={group.contractClause}
                  clausePage={group.clausePage}
                />
              ) : (
                group.errorMessage && (
                  <div style={{ color: "var(--p-muted)", fontSize: 13 }}>{group.errorMessage}</div>
                )
              )}
            </div>

            {/* decisions — keeps the per-row dropdown + bulk-selection widget as-is.
                key on the rule so the table re-seeds decision state on rule change. */}
            <div className="card-h" style={{ borderBottom: "none", paddingLeft: 0, paddingBottom: 4 }}>
              <h3>Decisions</h3>
              <span className="sub">{group.count} {group.count === 1 ? "policy" : "policies"}</span>
            </div>
            <ExceptionDecisionTable key={group.ruleKey} group={group} uploadId={uploadId}
              templateId={data?.output_template_id ?? undefined}
              contractId={data?.run?.contract_id ?? undefined}
              exportId={downloadId ?? undefined}
              onSaved={() => { setSavedThisSession(true); return load(); }}
              backLink={backLink} backState={backState} />
          </>
        )}
      </div>
    </div>
  );
}
