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
        card: "0 1px 2px rgba(17, 24, 39, 0.04), 0 1px 1px rgba(17, 24, 39, 0.03)",
      },
    },
  },
  plugins: [],
} satisfies Config;
