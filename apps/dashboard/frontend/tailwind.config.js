/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
    "./node_modules/@tremor/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        rn: {
          dark:    "#004A1C",
          med:     "#006B28",
          primary: "#009B3A",
          light:   "#66D998",
          bg:      "#F2FAF5",
        },
        sent: {
          fav:   "#2E8540",
          warn:  "#E08C2B",
          alert: "#C8372D",
        },
        tremor: {
          brand: {
            faint:    "#F2FAF5",
            muted:    "#66D998",
            subtle:   "#2E8540",
            DEFAULT:  "#009B3A",
            emphasis: "#006B28",
            inverted: "#ffffff",
          },
          background: {
            muted:    "#f9fafb",
            subtle:   "#f3f4f6",
            DEFAULT:  "#ffffff",
            emphasis: "#374151",
          },
          border:  { DEFAULT: "#e5e7eb" },
          ring:    { DEFAULT: "#e5e7eb" },
          content: {
            subtle:   "#9ca3af",
            DEFAULT:  "#6b7280",
            emphasis: "#374151",
            strong:   "#111827",
            inverted: "#ffffff",
          },
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 8px rgba(0,74,28,0.07)",
      },
    },
  },
  plugins: [],
};
