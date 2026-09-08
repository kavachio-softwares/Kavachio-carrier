/**
 * The file drop target, shared by every screen that takes a bordereau.
 *
 * Lifted out of DirectRun unchanged so the BROKER's Process Bordereau is the
 * same control as the carrier's, not a plain <input type="file"> that merely
 * does the same job. Two implementations would drift — one would get the
 * re-pick fix below and the other would not.
 */
import React, { useEffect, useRef, useState } from "react";

export function Dropzone({
  file, onPick, disabled, lockedReason,
  // What this drop target takes, and what it says it takes. Defaulted to the
  // bordereau formats so every existing caller is untouched — the contract
  // flow drops a WORDING here, which is a PDF, and would otherwise have needed
  // a second copy of this component. The header above says why that would be
  // the wrong move.
  accept = ".xlsx,.xls,.csv,.xml,.json",
  hint = ".xlsx, .xls, .csv",
  label,
}: {
  file: File | null; onPick: (f: File | null) => void; disabled?: boolean;
  lockedReason?: string; accept?: string; hint?: string; label?: React.ReactNode;
}) {
  const [drag, setDrag] = useState(false);
  const ref = useRef<HTMLInputElement>(null);
  // Whenever the selection is cleared (Clear button, Remove link, or a
  // carrier/program change), also reset the native input's value. Otherwise the
  // input keeps the old file path and re-picking the SAME file fires no change
  // event — so the file never re-selects and the button stays disabled.
  useEffect(() => { if (!file && ref.current) ref.current.value = ""; }, [file]);
  return (
    <div
      onClick={() => !disabled && ref.current?.click()}
      onDragOver={e => { e.preventDefault(); if (!disabled) setDrag(true); }}
      onDragLeave={() => setDrag(false)}
      onDrop={e => {
        e.preventDefault(); setDrag(false);
        if (disabled) return;
        const f = e.dataTransfer.files?.[0]; if (f) onPick(f);
      }}
      className={`drop lg${file ? " filled" : ""}${disabled ? " disabled" : ""}`}
      style={{
        cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.6 : 1,
        ...(drag ? { borderColor: "var(--p-primary)", background: "var(--p-primary-soft)" } : {}),
      }}>
      <input ref={ref} type="file" accept={accept} style={{ display: "none" }} disabled={disabled}
        onClick={e => e.stopPropagation()}
        onChange={e => onPick(e.target.files?.[0] ?? null)} />
      <svg className="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M14 3v4a1 1 0 0 0 1 1h4" /><path d="M5 3h9l5 5v13H5z" /><path d="M9 14h6M9 17h4" />
      </svg>
      {file ? (
        <div>
          <b>{file.name}</b>
          <div style={{ fontSize: 12, marginTop: 4 }}>
            Drag a new file to replace ·{" "}
            <span className="linkish" onClick={e => { e.stopPropagation(); onPick(null); if (ref.current) ref.current.value = ""; }}>Remove</span>
          </div>
        </div>
      ) : (
        <div>
          {label ?? <><b>Click to Upload</b> or Drag &amp; Drop</>}
          <div style={{ fontSize: 12, marginTop: 4 }}>{hint}</div>
        </div>
      )}
    </div>
  );
}
