/**
 * Per-model token pricing, in USD per million tokens.
 *
 * Savings are computed client-side rather than server-side on purpose: the
 * gateway records how many tokens the cache avoided sending upstream, but what
 * those tokens would have *cost* depends entirely on which model you would have
 * called. Switching the dropdown reprices the same token counts instantly.
 */

export interface ModelPricing {
  id: string;
  label: string;
  /** USD per 1M input (prompt) tokens. */
  inputPerMillion: number;
  /** USD per 1M output (completion) tokens. */
  outputPerMillion: number;
}

export const MODELS: ModelPricing[] = [
  {
    id: "mistral-small",
    label: "Mistral Small",
    inputPerMillion: 0.2,
    outputPerMillion: 0.6,
  },
  {
    id: "gpt-4o",
    label: "GPT-4o",
    inputPerMillion: 2.5,
    outputPerMillion: 10,
  },
  {
    id: "claude-sonnet",
    label: "Claude Sonnet",
    inputPerMillion: 3,
    outputPerMillion: 15,
  },
];

export const DEFAULT_MODEL_ID = MODELS[0].id;

export function getModel(id: string): ModelPricing {
  return MODELS.find((model) => model.id === id) ?? MODELS[0];
}

export function computeSavings(
  promptTokens: number,
  completionTokens: number,
  model: ModelPricing,
): { promptCost: number; completionCost: number; total: number } {
  const promptCost = (promptTokens / 1_000_000) * model.inputPerMillion;
  const completionCost = (completionTokens / 1_000_000) * model.outputPerMillion;

  return { promptCost, completionCost, total: promptCost + completionCost };
}

/**
 * Small savings are the common case early on, so never round a non-zero figure
 * down to "$0.00" — fall back to more decimals until something is visible.
 */
export function formatUsd(value: number): string {
  if (value === 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(5)}`;
  if (value < 1) return `$${value.toFixed(4)}`;

  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function formatTokens(value: number): string {
  return value.toLocaleString("en-US");
}
