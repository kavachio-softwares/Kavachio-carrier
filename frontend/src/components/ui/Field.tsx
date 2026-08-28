import { InputHTMLAttributes, SelectHTMLAttributes } from "react";

export function Field({ label, children }:
  { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
    </label>
  );
}
export function TextInput(p: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...p} className={`input ${p.className ?? ""}`} />;
}
export function Select(p: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...p} className={`input ${p.className ?? ""}`} />;
}
