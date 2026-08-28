import { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "ghost" | "accent" | "danger";

export function Button({
  variant = "primary",
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  const base =
    "inline-flex items-center justify-center gap-1.5 px-3.5 py-2 text-sm font-medium rounded-md transition disabled:opacity-50 disabled:cursor-not-allowed";
  const styles: Record<Variant, string> = {
    primary: "bg-navy text-white hover:bg-navy-dark active:bg-navy-dark",
    secondary: "bg-white border border-border text-ink hover:bg-surface-2",
    ghost: "bg-transparent text-ink hover:bg-surface-2",
    accent: "bg-accent text-white hover:opacity-90",
    danger: "bg-danger text-white hover:opacity-90",
  };
  return <button className={`${base} ${styles[variant]} ${className}`} {...rest} />;
}
export default Button;
