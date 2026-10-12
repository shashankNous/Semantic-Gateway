/**
 * Token pricing for the upstream models this gateway actually proxies to.
 *
 * Deliberately **not** a model picker. An earlier version let you reprice the
 * saved tokens as GPT-4o or Claude Sonnet, which put a dollar figure under a
 * vendor's name for traffic that never touched that vendor — the headline number
 * moved 12x depending on a dropdown, which is exactly what a cost figure must not
 * do. What the cache saved is a fact about the model that served the traffic, and
 * the gateway now reports which model that was (`stats.models`).
 *
 * Keys are the upstream model ids as recorded in `requests.model`, so a model with
 * no entry here is priced as unknown rather than silently as something else.
 */

export interface ModelPricing {
  label: string;
  /** USD per 1M input (prompt) tokens. */
  inputPerMillion: number;
  /** USD per 1M output (completion) tokens. */
  outputPerMillion: number;
}

export const MODEL_PRICING: Record<string, ModelPricing> = {
  "mistral-small-latest": {
    label: "Mistral Small",
    inputPerMillion: 0.2,
    outputPerMillion: 0.6,
  },
  "mistral-medium-latest": {
    label: "Mistral Medium",
    inputPerMillion: 0.4,
    outputPerMillion: 2,
  },
  "mistral-large-latest": {
    label: "Mistral Large",
    inputPerMillion: 2,
    outputPerMillion: 6,
  },
};

export interface ObservedModel {
  model: string;
  requests: number;
}

/** Pricing for a model id, or null when we have no published rate for it. */
export function getPricing(model: string | null): ModelPricing | null {
  if (!model) return null;
  return MODEL_PRICING[model] ?? null;
}

/**
 * The model that served most of this tenant's traffic, or null if the gateway
 * reported none. Mixed-model traffic is reported separately by the caller rather
 * than blended, since blending two price points produces a number that is true of
 * neither.
 */
export function dominantModel(models: ObservedModel[] | undefined): ObservedModel | null {
  if (!models || models.length === 0) return null;
  return models.reduce((best, current) =>
    current.requests > best.requests ? current : best,
  );
}

export function computeSavings(
  promptTokens: number,
  completionTokens: number,
  pricing: ModelPricing,
): { promptCost: number; completionCost: number; total: number } {
  const promptCost = (promptTokens / 1_000_000) * pricing.inputPerMillion;
  const completionCost = (completionTokens / 1_000_000) * pricing.outputPerMillion;

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
