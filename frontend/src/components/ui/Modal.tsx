import { ReactNode, useEffect } from "react";
import { X } from "lucide-react";

const SIZE_CLASS: Record<string, string> = {
  sm: "max-w-sm", md: "max-w-md", lg: "max-w-lg", xl: "max-w-xl",
  "2xl": "max-w-2xl", "3xl": "max-w-3xl", "4xl": "max-w-4xl",
};

export function Modal({
  open, title, onClose, children, footer, size = "lg",
}: {
  open: boolean;
  title?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  /** Max width of the dialog. Defaults to "lg" (unchanged from before). */
  size?: "sm" | "md" | "lg" | "xl" | "2xl" | "3xl" | "4xl";
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className={`relative z-10 w-full ${SIZE_CLASS[size] ?? SIZE_CLASS.lg}
        rounded-xl bg-white shadow-xl border border-border max-h-[90vh] flex flex-col`}>
        <header className="flex items-center justify-between px-5 py-3.5 border-b border-border">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose}
            className="text-ink-soft hover:text-ink transition" aria-label="Close">
            <X size={18} />
          </button>
        </header>
        <div className="px-5 py-4 overflow-y-auto">{children}</div>
        {footer && (
          <footer className="px-5 py-3 border-t border-border flex items-center justify-end gap-2">
            {footer}
          </footer>
        )}
      </div>
    </div>
  );
}
export default Modal;
