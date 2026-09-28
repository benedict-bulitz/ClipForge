import type { FinalQualityFix, FinalQualityIssue, FinalQualityRepair, FinalQualityReportEntry, FinalQualityReportStatus, FinalQualityReview } from "./types";

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

function capitalise(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/**
 * The critic's per-issue report.  Newer reviews carry it (the backend is the
 * only place that decides fixed / manual / unfixable); older reviews are
 * mapped once from their repairs and unresolved issues.
 */
export function qualityReport(review: FinalQualityReview | undefined | null): FinalQualityReportEntry[] {
  if (!review) return [];
  if (Array.isArray(review.report)) return review.report;
  const fixed: FinalQualityReportEntry[] = appliedQualityRepairs(review).map((repair, index) => ({
    id: `legacy-fixed-${repair.sceneId}-${index}`, issue_ids: [], scene_id: repair.sceneId, scene_number: repair.sceneNumber ?? 0, code: "legacy", category: "legacy",
    severity: "error", title: capitalise(repair.text), message: repair.text, status: "fixed", reason: null, reason_text: null, detail: null, fix: null,
  }));
  const remaining: FinalQualityReportEntry[] = unresolvedQualityScenes(review).map((scene) => ({
    id: `legacy-open-${scene.sceneId}`, issue_ids: [], scene_id: scene.sceneId, scene_number: scene.sceneNumber, code: "legacy", category: "legacy",
    severity: "error", title: scene.message, message: scene.message, status: "manual", reason: null, reason_text: scene.attempt ? capitalise(scene.attempt) : null, detail: null,
    fix: { kind: "change_media", label: "Change media", scene_id: scene.sceneId, scene_number: scene.sceneNumber, description: `Choose another visual for scene ${scene.sceneNumber}` },
  }));
  return [...fixed, ...remaining];
}

export type QualityCounts = Record<FinalQualityReportStatus, number>;

export function qualityCounts(review: FinalQualityReview | undefined | null): QualityCounts {
  const counts: QualityCounts = { fixed: 0, manual: 0, unfixable: 0 };
  for (const entry of qualityReport(review)) counts[entry.status] += 1;
  return counts;
}

const COUNT_TEXT: Record<FinalQualityReportStatus, (count: number) => string> = {
  fixed: (count) => `${count} fixed automatically`,
  manual: (count) => `${count} can be fixed manually`,
  unfixable: (count) => `${count} could not be safely repaired`,
};

/** Summary parts from the actual report, e.g. "2 fixed automatically" (zero counts are left out). */
export function qualitySummaryParts(review: FinalQualityReview | undefined | null): Array<{ status: FinalQualityReportStatus; text: string; count: number }> {
  const counts = qualityCounts(review);
  return (["fixed", "manual", "unfixable"] as const).filter((status) => counts[status] > 0).map((status) => ({ status, count: counts[status], text: COUNT_TEXT[status](counts[status]) }));
}

export const QUALITY_GROUP_TITLES: Record<FinalQualityReportStatus, string> = {
  fixed: "Fixed automatically",
  manual: "Needs your decision",
  unfixable: "Could not be fixed safely",
};

export type QualityGroupScene = { sceneId: string; sceneNumber: number; fix: FinalQualityFix | null; entryId: string };
export type QualityGroupItem = { key: string; title: string; note: string | null; scenes: QualityGroupScene[] };
export type QualityGroup = { status: FinalQualityReportStatus; title: string; items: QualityGroupItem[] };

/**
 * Report entries grouped for display: one row per issue type and explanation
 * (e.g. "Visual does not match the narration" — "…AI image budget is used up"
 * for scenes 3, 11, 12), each scene keeping its own Fix action.  Scenes whose
 * explanation differs stay in separate rows, so no difference is hidden.
 */
export function groupQualityReport(review: FinalQualityReview | undefined | null): QualityGroup[] {
  const groups = new Map<FinalQualityReportStatus, Map<string, QualityGroupItem>>();
  for (const entry of qualityReport(review)) {
    const note = entry.status === "fixed" ? (entry.detail ? capitalise(entry.detail) : null) : entry.reason_text;
    const key = `${entry.status}|${entry.title}|${note ?? ""}`;
    const items = groups.get(entry.status) ?? new Map<string, QualityGroupItem>();
    groups.set(entry.status, items);
    const item = items.get(key) ?? { key, title: entry.title, note, scenes: [] };
    items.set(key, item);
    if (!item.scenes.some((scene) => scene.sceneId === entry.scene_id)) {
      item.scenes.push({ sceneId: entry.scene_id, sceneNumber: entry.scene_number, fix: entry.fix?.kind === "change_media" ? entry.fix : null, entryId: entry.id });
    }
  }
  return (["fixed", "manual", "unfixable"] as const)
    .filter((status) => groups.has(status))
    .map((status) => ({
      status,
      title: QUALITY_GROUP_TITLES[status],
      items: [...groups.get(status)!.values()].map((item) => ({ ...item, scenes: [...item.scenes].sort((a, b) => a.sceneNumber - b.sceneNumber) })),
    }));
}

/** "Scene 3" / "Scenes 3, 11 and 12". */
export function sceneListLabel(numbers: number[]): string {
  if (numbers.length === 1) return `Scene ${numbers[0]}`;
  return `Scenes ${numbers.slice(0, -1).join(", ")} and ${numbers.at(-1)}`;
}

/** Per-scene marker for the scene list. */
export function sceneQualityState(review: FinalQualityReview | undefined | null, sceneId: string): "repaired" | "issue" | null {
  if (!review) return null;
  if (Array.isArray(review.report)) {
    const entries = review.report.filter((entry) => entry.scene_id === sceneId);
    if (entries.some((entry) => entry.status !== "fixed")) return "issue";
    return entries.length ? "repaired" : null;
  }
  if (remainingQualityIssues(review).some((issue) => issue.scene_id === sceneId && issue.severity === "error")) return "issue";
  if ((review.repairs ?? []).some((repair) => repair.scene_id === sceneId && repair.repair_effective)) return "repaired";
  return null;
}
