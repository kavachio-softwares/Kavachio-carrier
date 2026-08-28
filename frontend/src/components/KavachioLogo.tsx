// The official Kavachio mark. Rendered from the brand PNG in /public
// (Kavachio_main_logo.png) so the sidebar, loading overlay, login and reset
// screens all share one source of truth — swap the file to rebrand everywhere.
// The `color` prop is retained for call-site compatibility but no longer tints
// the mark (a PNG can't be recolored); callers currently rely on the default.
type Props = {
  size?: number;
  color?: string;
  className?: string;
  style?: React.CSSProperties;
};

// Served from /public at the site root by Vite.
const LOGO_SRC = "/Kavachio_main_logo.png";

export default function KavachioLogo({ size = 34, className, style }: Props) {
  return (
    <img
      src={LOGO_SRC}
      alt="Kavachio"
      className={className}
      width={size}
      height={size}
      style={{ objectFit: "contain", display: "block", ...style }}
    />
  );
}
