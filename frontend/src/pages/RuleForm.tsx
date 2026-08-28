import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { isKavachioAdmin } from "../auth";
import {
  RuleClass, getRuleCatalogue, getRule, createRule, updateRule,
} from "../api/ruleLibrary";

export default function RuleForm() {
  const nav = useNavigate();
  const { id } = useParams();
  const editing = !!id;
  const platform = isKavachioAdmin();

  const [classes, setClasses] = useState<RuleClass[]>([]);
  const [severities, setSeverities] = useState<string[]>([]);
  const [ruleName, setRuleName] = useState("");
  const [className, setClassName] = useState("");
  const [severity, setSeverity] = useState("Major");
  const [logic, setLogic] = useState("");
  const [isActive, setIsActive] = useState(true);
  // Label of the rule's stored type — only needed when editing a seeded rule
  // whose (legacy) type isn't one of the generic building blocks in the dropdown.
  const [currentLabel, setCurrentLabel] = useState("");

  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const cat = await getRuleCatalogue();
        setClasses(cat.classes);
        setSeverities(cat.severities);
        if (!editing && cat.classes[0]) setClassName(cat.classes[0].class_name);
        if (editing) {
          const r = await getRule(Number(id));
          if (!r) { setErr("This rule no longer exists."); return; }
          setRuleName(r.rule_name);
          setClassName(r.class_name);
          setCurrentLabel(r.class_label);
          setSeverity(r.severity);
          setLogic(r.validation_logic ?? "");
          setIsActive(r.is_active);
        }
      } catch (e: any) {
        setErr(e?.response?.data?.detail ?? "Couldn't load the form.");
      } finally { setLoading(false); }
    })();
  }, [id]);

  const canSave = ruleName.trim().length > 0 && className.length > 0 && !busy;

  async function save() {
    if (!canSave) return;
    setBusy(true); setErr(null);
    const body = {
      rule_name: ruleName.trim(),
      class_name: className,
      severity,
      validation_logic: logic.trim() || null,
      is_active: isActive,
    };
    try {
      if (editing) await updateRule(Number(id), body);
      else await createRule(body);
      nav("/rule-library");
    } catch (e: any) {
      setErr(e?.response?.data?.detail ?? "Couldn't save the rule.");
    } finally { setBusy(false); }
  }

  return (
    <div className="proto">
      <div className="view full">
        <div className="page-head">
          <div className="t">
            <h2>{editing ? "Edit Rule" : "New Rule"}</h2>
            <p>
              {platform
                ? "A generic rule that runs on every broker's BDX files."
                : "A generic rule that runs on all your BDX files."}
            </p>
          </div>
          <div className="actions">
            <button className="btn" onClick={() => nav("/rule-library")}>← Rules</button>
            <button className="btn pri" onClick={save} disabled={!canSave}
              title={canSave ? undefined : "Enter a rule name and pick a type first"}>
              {busy ? "Saving…" : editing ? "Save changes" : "Create rule"}
            </button>
          </div>
        </div>

        {err && <div className="note warn" style={{ marginBottom: 18 }}>{err}</div>}

        <div className="note" style={{ marginBottom: 18}}>
          A rule only works after you run <b>Bordereau Setup</b> for a program. If a program is already
          set up, run its setup again to pick up this rule.
        </div>

        {loading ? (
          <div className="card pad">Loading…</div>
        ) : (
          <div className="grid g-2">
            <div className="card pad">
              <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Rule</h3>
              <div className="field">
                <label>Rule name</label>
                <input value={ruleName} autoFocus placeholder="e.g. Insured ZIP must be valid"
                  onChange={e => setRuleName(e.target.value)} />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Description</label>
                <textarea value={logic} rows={4}
                  placeholder="In plain English, e.g. 'ZIP must be 5 digits.'"
                  onChange={e => setLogic(e.target.value)} />
                <div className="hint">
                  What the rule checks. For math rules, write the formula here.
                </div>
              </div>
            </div>

            <div className="card pad">
              <h3 style={{ margin: "0 0 16px", fontSize: 14 }}>Type &amp; severity</h3>
              <div className="field">
                <label>Rule type</label>
                <select value={className} onChange={e => setClassName(e.target.value)}>
                  {/* Editing a seeded rule whose type isn't a generic block: keep
                      its current value selectable so the type isn't lost on save. */}
                  {className && !classes.some(c => c.class_name === className) && (
                    <option value={className}>{currentLabel || className}</option>
                  )}
                  {classes.map(c => (
                    <option key={c.class_name} value={c.class_name}>{c.label}</option>
                  ))}
                </select>
                <div className="hint">
                  {classes.find(c => c.class_name === className)?.hint ?? "What kind of check to run."}
                </div>
              </div>
              <div className="field">
                <label>Severity</label>
                <select value={severity} onChange={e => setSeverity(e.target.value)}>
                  {severities.map(s => <option key={s} value={s}>{s}</option>)}
                </select>
                <div className="hint">Critical and Major fail the row. Minor is only a warning.</div>
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Status</label>
                <label style={{
                  display: "flex", alignItems: "center", gap: 10, cursor: "pointer",
                  padding: "10px 12px", border: "1px solid var(--p-border)", borderRadius: 8,
                }}>
                  <input type="checkbox" checked={isActive}
                    onChange={e => setIsActive(e.target.checked)}
                    style={{ width: 16, height: 16, margin: 0, flex: "none" }} />
                  <span>Active — run this rule on new uploads</span>
                </label>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
