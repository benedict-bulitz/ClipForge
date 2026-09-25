import type { TripleHook } from "./types";

export type TripleHookSummary = {
  verbal: string;
  onScreen: string;
  visual: string;
  strategy: string;
  meta: string;
};

const label = (value?: string | null) => (value ?? "").replaceAll("_", " ").trim();

/** Compact, debuggable view of the selected opening; null for projects without a V2 plan. */
export function tripleHookSummary(plan?: TripleHook | null): TripleHookSummary | null {
  if (!plan || (plan.version ?? 0) < 2 || !plan.verbal_hook) return null;
  const visual = plan.visual_hook ?? {};
  const visualText = [visual.framing, visual.subject || visual.visual_goal, visual.action_state].filter(Boolean).join(", ");
  const onScreen = plan.on_screen_text_hook?.trim()
    ? plan.on_screen_text_hook.trim()
    : `omitted${plan.on_screen_omitted_reason ? ` (${label(plan.on_screen_omitted_reason)})` : ""}`;
  const parts = [
    typeof plan.score === "number" ? `score ${Math.round(plan.score)}` : null,
    plan.selection?.candidate_count ? `${plan.selection.eligible_count ?? 0}/${plan.selection.candidate_count} candidates passed` : null,
    plan.status === "fallback" ? "fallback" : null,
    ...(plan.reason_codes ?? []).slice(0, 3).map(label),
  ].filter(Boolean);
  return {
    verbal: plan.verbal_hook,
    onScreen,
    visual: visualText || "—",
    strategy: label(plan.selected_strategy) || "—",
    meta: parts.join(" · "),
  };
}
