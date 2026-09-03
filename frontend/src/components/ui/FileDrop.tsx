/**
 * The dashed upload box, and the two tones it comes in.
 *
 * Every screen that asks for a document asks for it the same way — click or
 * drag, the name shown back with a tick, an ✕ to take it off again — so it is
 * defined once here rather than re-typed per screen. A file input that looks
 * different on two screens is not two designs; it is one design and a bug.
 *
 * TWO TONES, AND THEY MEAN ONE THING: must you, or may you.
 *
 * There used to be five — sky, indigo, amber, rose, teal — handed out by
 * grouping, so five boxes doing the same job arrived in three different colours
 * and none of them said anything. Worse, the group that happened to be rose
 * held both the REQUIRED contract and the OPTIONAL reference documents, so an
 * optional field was painted in the colour the rest of the app uses for a
 * problem.
 *
 * Now the colour answers the only question a person asks of an empty upload
 * box: do I have to fill this in? Required carries the brand tint, optional
 * stays quiet grey, and a filled box turns green whichever it was.
 */
import { ReactNode, useEffect, useRef, useState } from "react";
import { CheckCircle2, UploadCloud } from "lucide-react";

export const DROP_TONES = {
  required: {
    idle: "border-navy/30 bg-navy/[0.04] hover:border-navy/60 hover:bg-navy/[0.07]",
    icon: "text-navy", cta: "text-navy",
  },
  optional: {
    idle: "border-border bg-surface-2/40 hover:border-ink-soft hover:bg-surface-2",
    icon: "text-ink-soft", cta: "text-ink-muted",
  },
} as const;
export type DropTone = keyof typeof DROP_TONES;

/** The word behind the tone. A colour alone is a convention the reader has to
 *  learn; the word is the same answer said out loud, and it survives being
 *  printed, screenshotted or read by someone who does not see the tint. */
export function DropBadge({ required }: { required?: boolean }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-[9.5px] font-semibold uppercase tracking-wide
      ${required ? "bg-navy/10 text-navy" : "bg-surface-2 text-ink-soft"}`}>
      {required ? "Required" : "Optional"}
    </span>
  );
}

/** The box itself: border state, drag handling and the click target. Shared by
 *  the single- and multi-file variants so the two can never drift apart. */
function Zone({ tone, filled, disabled, onFiles, multiple, accept, inputRef, children }: {
  tone: DropTone;
  filled: boolean;
  disabled?: boolean;
  onFiles: (fs: File[]) => void;
  multiple?: boolean;
  accept?: string;
  inputRef: React.RefObject<HTMLInputElement>;
  children: ReactNode;
}) {
  const [drag, setDrag] = useState(false);
  const t = DROP_TONES[tone];
  return (
    <div
      onClick={() => { if (!disabled) inputRef.current?.click(); }}
      onDragOver={e => { e.preventDefault(); if (!disabled) setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={e => {
        e.preventDefault(); setDrag(false);
        if (disabled) return;
        const fs = Array.from(e.dataTransfer.files || []);
        if (fs.length) onFiles(multiple ? fs : fs.slice(0, 1));
      }}
      className={`rounded-lg border-2 border-dashed p-4 text-center transition select-none
        ${disabled ? "cursor-not-allowed opacity-50 border-border bg-surface-2"
          : `cursor-pointer ${drag ? "border-navy bg-navy/5"
              : filled ? "border-emerald-300 bg-emerald-50/40" : t.idle}`}`}>
      <input
        ref={inputRef} type="file" className="hidden" disabled={disabled}
        multiple={multiple} accept={accept}
        onClick={e => e.stopPropagation()}
        onChange={e => {
          const fs = Array.from(e.target.files || []);
          // Multi keeps adding across rounds, so its input is cleared to let the
          // SAME file be re-picked; single keeps its value, matching the native
          // control.
          if (multiple) e.target.value = "";
          if (fs.length) onFiles(fs);
          else if (!multiple) onFiles([]);
        }} />
      {children}
    </div>
  );
}

/** Header line shared by both variants: icon, label, required/optional badge. */
function Head({ icon, label, tone, filled, required }: {
  icon: ReactNode; label: string; tone: DropTone; filled: boolean; required?: boolean;
}) {
  return (
    <div className="mb-1.5 flex flex-wrap items-center justify-center gap-1.5 text-sm font-medium">
      <span className={filled ? "text-emerald-600" : DROP_TONES[tone].icon}>{icon}</span>
      {label}
      <DropBadge required={required} />
    </div>
  );
}

function Prompt({ tone, hint }: { tone: DropTone; hint?: string }) {
  return (
    <div className="text-[11px] text-ink-muted">
      <span className={`inline-flex items-center gap-1 font-medium ${DROP_TONES[tone].cta}`}>
        <UploadCloud size={12} /> Click to Upload
      </span> or Drag &amp; Drop
      {hint ? <div className="mt-0.5 opacity-80">{hint}</div> : null}
    </div>
  );
}

/** ONE file. */
export function FileDrop({
  label, icon, file, onPick, accept, hint, tone, required, disabled, disabledNote,
}: {
  label: string;
  icon: ReactNode;
  file: File | null;
  onPick: (f: File | null) => void;
  accept?: string;
  hint?: string;
  tone: DropTone;
  required?: boolean;
  disabled?: boolean;
  /** What to say INSTEAD of the upload prompt while the box is shut — the
   *  reason is always local to the screen, so the screen supplies it. */
  disabledNote?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  // The native input keeps its own value, so when the file is cleared from the
  // outside it has to be cleared too — otherwise re-picking the SAME file fires
  // no change event and silently attaches nothing.
  useEffect(() => { if (!file && ref.current) ref.current.value = ""; }, [file]);

  return (
    <Zone tone={tone} filled={!!file} disabled={disabled} accept={accept} inputRef={ref}
      onFiles={fs => onPick(fs[0] ?? null)}>
      <Head icon={icon} label={label} tone={tone} filled={!!file} required={required} />
      {file ? (
        <div className="flex items-center justify-center gap-1.5 text-[11px] text-emerald-700">
          <CheckCircle2 size={12} className="shrink-0" />
          <span className="min-w-0 truncate">{file.name}</span>
          <button className="ml-0.5 shrink-0 text-ink-muted hover:text-danger"
            onClick={e => {
              e.stopPropagation(); onPick(null);
              if (ref.current) ref.current.value = "";
            }}>✕</button>
        </div>
      ) : disabled ? (
        <div className="text-[11px] text-ink-muted">{disabledNote ?? "Not available yet"}</div>
      ) : (
        <Prompt tone={tone} hint={hint} />
      )}
    </Zone>
  );
}

/** MANY files, added over rounds and de-duplicated by name + size, so
 *  re-picking one does not stack a second copy of it. */
export function MultiFileDrop({
  label, icon, files, onChange, accept, hint, tone, required, disabled, disabledNote,
}: {
  label: string;
  icon: ReactNode;
  files: File[];
  onChange: (fs: File[]) => void;
  accept?: string;
  hint?: string;
  tone: DropTone;
  required?: boolean;
  disabled?: boolean;
  disabledNote?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  return (
    <Zone tone={tone} filled={files.length > 0} disabled={disabled} accept={accept}
      multiple inputRef={ref}
      onFiles={fs => {
        const seen = new Set(files.map(f => `${f.name}|${f.size}`));
        onChange([...files, ...fs.filter(f => !seen.has(`${f.name}|${f.size}`))]);
      }}>
      <Head icon={icon} label={label} tone={tone} filled={files.length > 0} required={required} />
      {files.length > 0 ? (
        <div className="space-y-1">
          {files.map((f, i) => (
            <div key={`${f.name}|${f.size}|${i}`}
              className="flex items-center justify-center gap-1.5 text-[11px] text-emerald-700">
              <CheckCircle2 size={12} className="shrink-0" />
              <span className="min-w-0 truncate">{f.name}</span>
              <button className="ml-0.5 shrink-0 text-ink-muted hover:text-danger"
                onClick={e => {
                  e.stopPropagation();
                  onChange(files.filter((_, j) => j !== i));
                }}>✕</button>
            </div>
          ))}
          <div className={`inline-flex items-center gap-1 pt-0.5 text-[11px] font-medium
            ${DROP_TONES[tone].cta}`}>
            <UploadCloud size={12} /> Add More
          </div>
        </div>
      ) : disabled ? (
        <div className="text-[11px] text-ink-muted">{disabledNote ?? "Not available yet"}</div>
      ) : (
        <Prompt tone={tone} hint={hint} />
      )}
    </Zone>
  );
}
