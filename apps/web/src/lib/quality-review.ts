import type { FinalQualityIssue, FinalQualityRepair, FinalQualityReview } from "./types";

export type QualityReviewTone = "passed" | "repaired" | "attention" | "muted";

/** Compact one-line state of the Final Video Critic for the finished video. */
export function qualityReviewSummary(review: FinalQualityReview | undefined | null, renderRevision?: number): { label: string; tone: QualityReviewTone } | null {
  if (!review || review.status === "disabled") return null;
  const label = review.summary?.label ?? (review.status === "passed" ? "Passed" : "Not reviewed");
  // A later edit (e.g. Change Media) produced a newer render than the one reviewed.
  if (renderRevision !== undefined && review.revision !== undefined && review.revision !== renderRevision) return { label: `${label} (earlier render)`, tone: "muted" };
  if (review.status === "issues_remain") return { label, tone: "attention" };
  if (review.status === "repaired") return { label, tone: "repaired" };
  if (review.status === "passed" || review.status === "passed_with_warnings") return { label, tone: "passed" };
  return { label, tone: "muted" };
}

/** Issues still visible in the final render, errors first. */
export function remainingQualityIssues(review: FinalQualityReview | undefined | null): FinalQualityIssue[] {
  const unresolved = review?.unresolved ? new Set(review.unresolved) : null;
  const issues = (review?.issues ?? []).filter((issue) => (unresolved ? unresolved.has(issue.id) : issue.severity === "error" || issue.severity === "warning"));
  return [...issues].sort((a, b) => (a.severity === b.severity ? a.scene_number - b.scene_number : a.severity === "error" ? -1 : 1));
}

const FALLBACK_REPAIR_TEXT: Record<FinalQualityRepair["action"], string> = {
  replace_media: "replaced a weak visual",
  convert_graphic_to_overlay: "turned the full-screen graphic into an overlay on a real visual",
  continue_base_visual: "kept the fact's visual for continuity",
  adjust_composition: "adjusted the composition",
  report_only: "reported only",
};

/** Repairs that measurably worked (the triggering issue is gone from the new render). */
export function appliedQualityRepairs(review: FinalQualityReview | undefined | null): Array<{ sceneId: string; sceneNumber: number | null; text: string }> {
  return (review?.repairs ?? [])
    .filter((repair) => repair.repair_effective)
    .map((repair) => ({ sceneId: repair.scene_id, sceneNumber: repair.scene_number ?? null, text: repair.result_message ?? FALLBACK_REPAIR_TEXT[repair.action] }));
}

export type UnresolvedScene = { sceneId: string; sceneNumber: number; message: string; attempt: string | null; issueCount: number };

/** One line per scene that still needs attention, with what the automatic repair tried. */
export function unresolvedQualityScenes(review: FinalQualityReview | undefined | null): UnresolvedScene[] {
  const scenes = new Map<string, UnresolvedScene>();
  for (const issue of remainingQualityIssues(review)) {
    const current = scenes.get(issue.scene_id);
    if (current) {
      current.issueCount += 1;
      continue;
    }
    const failed = (review?.repairs ?? []).find((repair) => repair.scene_id === issue.scene_id && !repair.repair_effective && (repair.repair_attempted || repair.blocked_reason));
    scenes.set(issue.scene_id, { sceneId: issue.scene_id, sceneNumber: issue.scene_number, message: issue.message, attempt: failed?.result_message ?? null, issueCount: 1 });
  }
  return [...scenes.values()];
}

/** Per-scene marker for the scene list. */
export function sceneQualityState(review: FinalQualityReview | undefined | null, sceneId: string): "repaired" | "issue" | null {
  if (!review) return null;
  if (remainingQualityIssues(review).some((issue) => issue.scene_id === sceneId && issue.severity === "error")) return "issue";
  if ((review.repairs ?? []).some((repair) => repair.scene_id === sceneId && repair.repair_effective)) return "repaired";
  return null;
}
