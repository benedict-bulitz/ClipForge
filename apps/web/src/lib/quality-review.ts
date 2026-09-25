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

/** Problems the viewer may still see, most severe first (errors, then warnings). */
export function remainingQualityIssues(review: FinalQualityReview | undefined | null): FinalQualityIssue[] {
  const issues = (review?.issues ?? []).filter((issue) => issue.severity === "error" || issue.severity === "warning");
  return [...issues].sort((a, b) => (a.severity === b.severity ? a.scene_number - b.scene_number : a.severity === "error" ? -1 : 1));
}

const ACTION_LABELS: Record<FinalQualityRepair["action"], string> = {
  replace_media: "replaced the visual",
  convert_graphic_to_overlay: "turned the full-screen graphic into an overlay on a real visual",
  continue_base_visual: "kept the fact's visual for continuity",
  adjust_composition: "adjusted the composition",
  report_only: "reported only",
};

function compositionLabel(repair: FinalQualityRepair): string {
  const parts: string[] = [];
  const overlay = repair.adjustments?.overlay;
  if (overlay?.mode === "remove") parts.push("removed the overlay");
  else if (overlay?.mode === "compact") parts.push("simplified the overlay");
  if (overlay?.placement) parts.push(`moved the overlay ${overlay.placement === "upper" ? "up" : "down"}`);
  if (repair.adjustments?.crop) parts.push("re-centred the crop");
  if (repair.adjustments?.motion === "static") parts.push("removed the camera motion");
  return parts.join(", ") || ACTION_LABELS.adjust_composition;
}

/** Automatic changes the user should know about ("Scene 2: simplified the overlay"). */
export function appliedQualityRepairs(review: FinalQualityReview | undefined | null): Array<{ sceneId: string; sceneNumber: number | null; text: string; outcome?: FinalQualityRepair["outcome"] }> {
  return (review?.repairs ?? [])
    .filter((repair) => repair.status === "applied")
    .map((repair) => ({
      sceneId: repair.scene_id,
      sceneNumber: repair.scene_number ?? null,
      text: repair.action === "adjust_composition" ? compositionLabel(repair) : ACTION_LABELS[repair.action],
      outcome: repair.outcome,
    }));
}

/** Per-scene marker for the scene list. */
export function sceneQualityState(review: FinalQualityReview | undefined | null, sceneId: string): "repaired" | "issue" | null {
  if (!review) return null;
  if ((review.issues ?? []).some((issue) => issue.scene_id === sceneId && issue.severity === "error")) return "issue";
  if ((review.changed_scenes ?? []).includes(sceneId)) return "repaired";
  return null;
}
