import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        navy: {
          DEFAULT: "#077282",
          dark: "#065E6B",
          light: "#3D9BAA",
        },
        ink: "#1A2032",
        "ink-muted": "#6B7280",
        "ink-soft": "#9CA3AF",
        surface: "#FFFFFF",
        "surface-2": "#F3F4F6",
        bg: "#F6F7F9",
        accent: "#03A2A6",
        success: "#16A34A",
        warn: "#D97706",
        danger: "#DC2626",
        border: "#E5E7EB",
      },
      fontFamily: {
        sans: ['"Montserrat"', "system-ui", "sans-serif"],
      },
      boxShadow: {
        // A card has to sit ON the page, not blend into it. The previous
        // pair of 1px shadows at 3-4% was close enough to nothing that
        // every card read as a flat white region of the background.
        card: "0 1px 2px rgba(17, 24, 39, 0.06), 0 6px 16px -8px rgba(17, 24, 39, 0.12)",
      },
    },
  },
  plugins: [],
} satisfies Config;
