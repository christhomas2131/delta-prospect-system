/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        mono: ["'JetBrains Mono'", "'Fira Code'", "Consolas", "monospace"],
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      colors: {
        bg: { DEFAULT: "#0a0c0f", card: "#111418", border: "#1e2530", hover: "#161b24" },
        accent: { DEFAULT: "#1e6fd4", dim: "#1558a8" },
        text: { primary: "#e2e8f0", secondary: "#8fa3bf", muted: "#4a5a70" },
        pressure: {
          operational: "#f97316",
          cost: "#ef4444",
          safety: "#eab308",
          governance: "#a855f7",
          environmental: "#22c55e",
          market: "#3b82f6",
          workforce: "#14b8a6",
        },
        strength: {
          strong: "#f8fafc",
          moderate: "#94a3b8",
          weak: "#475569",
        },
      },
    },
  },
  plugins: [],
}
