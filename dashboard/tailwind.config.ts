import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        // Authentic teal. `brand` is the surface colour; `deep` is the page
        // ground beneath it, `light`/`bright` are for borders and hovers.
        brand: {
          DEFAULT: "#035352",
          deep: "#012322",
          dark: "#02403E",
          light: "#046B6A",
          bright: "#0A8C89",
        },
        // Sidecar yellow. Doubles as the primary text colour on teal — opacity
        // modifiers (text-sidecar/60) cover every muted variant.
        sidecar: {
          DEFAULT: "#F3E8BC",
          dim: "#D9CFA6",
          deep: "#C4B173",
        },
      },
    },
  },
  plugins: [],
};
export default config;
