/**
 * "Which output template applies here?" — answered in place, on both the setup
 * screen and the run screen.
 *
 * The interesting case is the middle one. A template can be found WITHOUT being
 * this contract's: the programme has one, and until somebody makes a more
 * specific one that is what a run would use. Saying so plainly — "this is the
 * programme's template, not this contract's" — while there is still time to
 * make one is the entire reason this shows the match level rather than a tick.
 */
import { AlertTriangle, CheckCircle2, FileSpreadsheet, Plus } from "lucide-react";
import Button from "./ui/Button";
import type { ResolveResult } from "../api/outputTemplate";

const SOURCE_LABEL: Record<string, string> = {
  uploaded: "built from an uploaded sample workbook",
  standard: "built from a reporting standard",
  contract: "built from the contract",
};

/** Where the answer came from — not the same question as how it was built.
 *  "Nothing here is from this session" is the thing a user needs told when a
 *  saved setup's template appears on a screen they have not touched yet. */
const FOUND_VIA: Record<string, string> = {
  contract: "agreed for this contract",
  broker: "agreed for this broker",
  programme: "already saved against this programme",
};

/** Is the match less specific than what the user has selected? */
export function isBroaderThanScope(r: ResolveResult | null): boolean {
  if (!r?.found) return false;
  if (r.scope.contract_id && r.match_level !== "contract") return true;
  if (r.scope.broker_party_id && r.match_level === "programme") return true;
  return false;
}

export default function OutputTemplateState({
  resolving, resolved, disabled, uploading, onCreate, onOpen, compact, hideMissing,
}: {
  resolving: boolean;
  resolved: ResolveResult | null;
  disabled?: boolean;
  /** A sample workbook is staged, so a new template is about to be created from it. */
  uploading?: boolean;
  onCreate: () => void;
  onOpen?: (templateId: number) => void;
  /** Run screen: no create button, just the state. */
  compact?: boolean;
  /** The screen already offers both ways to fix it, so saying "not configured"
   *  here as well reads as a fault rather than as the choice it is. */
  hideMissing?: boolean;
}) {
  if (disabled) return null;

  if (uploading) {
    return (
      <Box tone="info">
        A new output template will be created from the file you uploaded.
        {resolved?.found && (
          <> It becomes the next version of <b>{resolved.template!.name}</b>.</>
        )}
      </Box>
    );
  }

  if (resolving) {
    return <Box tone="muted">Checking which output template applies…</Box>;
  }

  if (!resolved?.found) {
    if (hideMissing) return null;
    return (
      <Box tone="warn" icon={<AlertTriangle size={14} />}>
        <div className="font-medium">Output BDX Template not configured</div>
        <div className="mt-0.5">
          Nothing has been agreed for this
          {resolved?.scope.contract_id ? " contract" :
           resolved?.scope.broker_party_id ? " broker" : " programme"} yet.
          Upload the layout you have been asked for, or create one.
        </div>
        {!compact && (
          <Button className="mt-2" variant="secondary" onClick={onCreate}>
            <Plus size={14} /> Create Output BDX Template
          </Button>
        )}
      </Box>
    );
  }

  const t = resolved.template!;
  const broader = isBroaderThanScope(resolved);
  // The setup that would run here was built against a DIFFERENT template. Its
  // mapping belongs to that one, so this scope needs a setup of its own.
  const mismatch = !!resolved.setup && !resolved.setup.matches;
  return (
    <Box tone={broader || mismatch ? "warn" : "ok"}
      icon={broader || mismatch ? <AlertTriangle size={14} />
        : <CheckCircle2 size={14} />}>
      <div className="flex items-start gap-2">
        <FileSpreadsheet size={14} className="mt-0.5 shrink-0 opacity-70" />
        <div className="min-w-0">
          <div className="font-medium truncate">{t.name}</div>
          <div className="mt-0.5">
            v{t.version} · {t.output_format.toUpperCase()} ·{" "}
            {resolved.match_level
              ? FOUND_VIA[resolved.match_level] ?? resolved.match_level
              : "already saved"}
            {t.standard_meta?.jurisdiction && (
              <> · {t.standard_meta.standard} {t.standard_meta.jurisdiction}</>
            )}
          </div>
          <div className="text-[10.5px] opacity-80 mt-0.5">
            {SOURCE_LABEL[t.source_kind] ?? t.source_kind} — nothing you upload
            here is needed unless you want to replace it.
          </div>
          {broader && !mismatch && (
            <div className="mt-1">
              This is the <b>{resolved.match_level}</b>'s template, not one made
              for the selection above. It is what a run would use — make a more
              specific one if this contract reports differently.
            </div>
          )}
          {mismatch && (
            <div className="mt-1">
              The setup that runs here — <b>{resolved.setup!.name}</b> — was
              built against <b>{resolved.setup!.output_template_name}</b>, not
              this one. A setup learns its mapping from a single output
              template, so build the setup below against this scope to use it.
            </div>
          )}
        </div>
      </div>
      {!compact && (
        <div className="flex gap-2 mt-2">
          {onOpen && (
            <Button variant="secondary" onClick={() => onOpen(t.id)}>
              Review fields
            </Button>
          )}
          <Button variant="secondary" onClick={onCreate}>
            <Plus size={14} /> {broader ? "Create one for this scope" : "Replace it"}
          </Button>
        </div>
      )}
    </Box>
  );
}

function Box({ tone, icon, children }: {
  tone: "ok" | "warn" | "info" | "muted";
  icon?: React.ReactNode; children: React.ReactNode;
}) {
  const cls = {
    ok: "border-emerald-200 bg-emerald-50 text-emerald-800",
    warn: "border-amber-300 bg-amber-50 text-amber-800",
    info: "border-sky-200 bg-sky-50 text-sky-800",
    muted: "border-border bg-surface-2 text-ink-muted",
  }[tone];
  return (
    <div className={`rounded-md border p-2.5 text-[11.5px] leading-relaxed ${cls}`}>
      <div className="flex items-start gap-1.5">
        {icon && <span className="mt-0.5 shrink-0">{icon}</span>}
        <div className="min-w-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
