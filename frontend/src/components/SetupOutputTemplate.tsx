/**
 * The OUTPUT side of a Bordereau Setup, on the setup's own screen.
 *
 * A setup has two halves and the tabs only showed one: Field mapping answers
 * "which broker column feeds which output column", but not "what does the file
 * we deliver actually look like". That answer lives on the output template, one
 * screen away, and going there means losing the setup you were reading. So the
 * template's own grid is drawn here, against the template this setup generates
 * with — the SAME component the template screen uses, so there is one picture
 * of a layout rather than two that can drift.
 *
 * WHY THERE IS A SAVE BUTTON for something already written to the server.
 * Adding a column writes it immediately, but not always to the template you
 * were looking at: once a file has been generated from a version, the server
 * copies that version and writes the change to the copy, leaving the original
 * untouched so past downloads keep describing themselves correctly. The setup
 * still points at the original. So the column IS saved, and this setup still
 * would not deliver it — which is the gap that made an edit look like it had
 * vanished on the way to the next tab.
 *
 * Saving here is therefore one specific thing, and it says so: point THIS setup
 * at the version the edits are in. `shownId` is held by the parent page for the
 * same reason — a tab switch must not throw away a version just created.
 */
import { useCallback, useEffect, useState } from "react";
import { Layers, Save } from "lucide-react";
import { api } from "../api/client";
import { Card } from "./ui/Card";
import { Button } from "./ui/Button";
import TemplateSheetPreview, { type PreviewSheet } from "./TemplateSheetPreview";
import { getTemplateFields, type FieldsDoc } from "../api/outputTemplate";
import { errText } from "../utils/directSetup";

type TemplateResp = {
  id: number; name: string; version?: number; approved: boolean;
  output_format?: string; structure: { sheets: PreviewSheet[] };
};

export default function SetupOutputTemplate({
  boundId, templateName, shownId, onShown, onSave, editable = false,
}: {
  /** The template THIS SETUP generates with. */
  boundId: number | null;
  /** What the setup calls it, shown while the template itself is loading. */
  templateName?: string | null;
  /** The template on screen — the bound one, or a version an edit created. */
  shownId?: number | null;
  onShown?: (id: number) => void;
  /** Point the setup at `id`. Given only where this screen may edit. */
  onSave?: (id: number) => Promise<void>;
  editable?: boolean;
}) {
  const id = shownId ?? boundId;
  const [tpl, setTpl] = useState<TemplateResp | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  // Set once an edit has been made in this sitting, so the note below can tell
  // "you changed something and have not saved it" from "you are simply looking
  // at a different version".
  const [edited, setEdited] = useState(false);
  // WHERE THIS LAYOUT CAME FROM. A template is the same object however it was
  // made, so the grid below never changes — but what the columns MEAN does. One
  // built from the contract is the clauses' own list; one built from a standard
  // is that standard's published list; one uploaded is simply the columns of
  // the file somebody supplied, and nothing about the contract explains it. Not
  // saying which leaves the reader to assume the contract every time.
  const [doc, setDoc] = useState<FieldsDoc | null>(null);
  const unsaved = id != null && boundId != null && id !== boundId;

  const load = useCallback((want: number | null) => {
    if (want == null) return;
    api.get<TemplateResp>(`/export/template/${want}`)
      .then(r => { setTpl(r.data); setErr(null); })
      .catch(e => setErr(errText(e)));
  }, []);
  useEffect(() => { load(id); }, [load, id]);
  useEffect(() => {
    setDoc(null);
    if (id == null) return;
    let stale = false;
    // Provenance only — the grid does not wait on it, and a template that
    // cannot answer simply says nothing rather than blocking the tab.
    getTemplateFields(id).then(d => { if (!stale) setDoc(d); }).catch(() => {});
    return () => { stale = true; };
  }, [id]);

  /** After an edit. `nextId` is set only where the server made a new version. */
  function changed(nextId?: number) {
    setEdited(true); setSaved(false);
    if (nextId != null) { onShown?.(nextId); return; }   // the load follows `id`
    load(id);
  }

  async function save() {
    if (id == null || !onSave) return;
    setSaving(true); setErr(null);
    try {
      await onSave(id);
      setSaved(true); setEdited(false);
    } catch (e: unknown) { setErr(errText(e)); }
    finally { setSaving(false); }
  }

  if (boundId == null) {
    return (
      <Card>
        <div className="text-sm text-ink-muted">
          This setup has no output template yet, so there is no output layout to show.
        </div>
      </Card>
    );
  }

  return (
    <Card title={<span className="flex items-center gap-2">
      <Layers size={16} className="text-navy" />
      Output BDX · {tpl?.name ?? templateName ?? "template"}
      {tpl?.version != null && (
        <span className="text-xs font-normal text-ink-muted">v{tpl.version}</span>
      )}
    </span>}>
      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        <span>
          The file this setup delivers — active columns only, in delivery order,
          under the names the file will carry.
        </span>
        {tpl && (
          <span className={`pill ${tpl.approved ? "pill-green" : "pill-amber"}`}>
            {tpl.approved ? "Approved" : "Draft"}
          </span>
        )}
        {doc?.locked_by_history && (
          <span className="pill pill-amber" title="Past downloads describe themselves with this version">
            Already used — edits make a new version
          </span>
        )}
      </div>

      {doc && (
        <p className="mb-3 text-[12px] text-ink-muted">{provenance(doc)}</p>
      )}

      {err && (
        <div className="mb-3 rounded-md border border-red-200 bg-red-50 px-3 py-2
          text-[12px] text-red-700">{err}</div>
      )}

      {/* The one thing a person needs told here: the column is written, but this
          setup will not deliver it until the setup is pointed at the version it
          is in. */}
      {editable && unsaved && (
        <div className="mb-3 flex flex-wrap items-center gap-3 rounded-md border
          border-amber-200 bg-amber-50 px-3 py-2.5 text-[12px] text-amber-800">
          <span className="flex-1 min-w-[260px]">
            {edited
              ? "Your change was saved as a NEW version, because files have already "
                + "been generated from the old one — which is left exactly as it was. "
                + "This setup still delivers the old version until you save."
              : "You are looking at a version this setup does not use yet."}
          </span>
          <Button onClick={save} disabled={saving}>
            <Save size={14} /> {saving ? "Saving…" : "Save to this setup"}
          </Button>
        </div>
      )}
      {editable && saved && !unsaved && (
        <div className="mb-3 rounded-md border border-emerald-200 bg-emerald-50 px-3
          py-2 text-[12px] text-emerald-800">
          Saved — this setup now generates with this version.
        </div>
      )}

      {!tpl ? (
        // The grid says "no columns switched on yet" for an empty sheet list,
        // which is the wrong answer to give while the template is still coming.
        <div className="text-sm text-ink-muted">
          {err ? "Could not load this output template." : "Loading the output layout…"}
        </div>
      ) : (
        <TemplateSheetPreview sheets={tpl.structure?.sheets ?? []}
          {...(editable && id != null
            ? { templateId: id, onChanged: changed } : {})} />
      )}

      {editable && (
        <p className="mt-2 text-[11.5px] text-ink-soft">
          Columns belong to the template, not to this setup — every setup pointed
          at this version delivers them.
        </p>
      )}
    </Card>
  );
}

/** One line saying where these columns came from, in the reader's words. */
function provenance(doc: FieldsDoc): string {
  const t = doc.template;
  const std = doc.standard ?? t.standard_meta;
  const label = std
    ? [std.standard ?? std.standard_id, std.jurisdiction].filter(Boolean).join(" · ")
    : null;
  const contract = t.scope_names?.contract;
  switch (t.source_kind) {
    case "contract":
      return `Built from the contract${contract ? ` (${contract})` : ""} — these are the `
        + `columns its clauses ask for`
        + `${label ? `, with ${label} filling in what contracts never name.` : "."}`;
    case "standard":
      return `Built from ${label ?? "a reporting standard"} — its published column list. `
        + "The contract adds nothing here; it is only used to check the values.";
    default:
      return "Built from a sample workbook somebody supplied — these are that file's "
        + "own columns, added by hand rather than read from the contract or a "
        + "reporting standard.";
  }
}
