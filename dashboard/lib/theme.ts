/**
 * Brand palette, in one place.
 *
 * Tailwind classes cover the markup; Recharts takes raw colour strings for its
 * SVG, so both read from here to stay in sync. Keep these values identical to
 * the `brand` / `sidecar` scales in tailwind.config.ts.
 */

/** Authentic teal — the brand colour, used as the card surface. */
export const AUTHENTIC_TEAL = "#035352";

/** Sidecar yellow — the accent, and the primary text colour on teal. */
export const SIDECAR_YELLOW = "#F3E8BC";

/** Page ground: authentic teal taken most of the way to black. */
export const BRAND_DEEP = "#012322";

/**
 * Sidecar at low alpha, so chart furniture recedes without introducing new hues.
 *
 * `axis` colours the tick *labels* (the axis and tick lines are switched off),
 * so it is real text and has to clear 4.5:1 against the teal card — 0.75 alpha
 * lands at 4.8:1. Grid and cursor are purely decorative and can go much fainter.
 */
export const CHART = {
  line: SIDECAR_YELLOW,
  grid: "rgba(243, 232, 188, 0.12)",
  axis: "rgba(243, 232, 188, 0.75)",
  cursor: "rgba(243, 232, 188, 0.25)",
} as const;
