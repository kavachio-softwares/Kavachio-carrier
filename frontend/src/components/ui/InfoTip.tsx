import { ReactNode, useState } from "react";
import { Info } from "lucide-react";

// Small hover/focus info icon + tooltip bubble, for a one-line heading that
// needs a longer explanation without permanently taking up page space. Cards
// in this design system don't clip overflow (unlike the .proto one), so a
// plain absolutely-positioned bubble is enough — no portal needed.
export function InfoTip({ text }: { text: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative inline-flex"
      onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)} onBlur={() => setOpen(false)}>
      <button type="button" aria-label="More info"
        className="inline-flex items-center justify-center text-ink-soft hover:text-navy transition">
        <Info size={13} />
      </button>
      {open && (
        <span role="tooltip"
          className="absolute z-50 left-1/2 -translate-x-1/2 bottom-full mb-2 w-72 rounded-lg
            bg-navy text-white text-[11.5px] leading-relaxed px-3 py-2.5 shadow-lg pointer-events-none">
          {text}
          <span className="absolute left-1/2 -translate-x-1/2 top-full h-0 w-0
            border-4 border-transparent border-t-navy" />
        </span>
      )}
    </span>
  );
}
export default InfoTip;
