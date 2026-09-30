import type { GenerationJob, ProjectOverview } from "./types";

export function activeQueueJobs(jobs: GenerationJob[]): GenerationJob[] {
  // A cancelling job still holds the one worker until it reaches its next checkpoint.
  const running = jobs.filter((job) => job.status === "running" || job.status === "cancelling");
  const waiting = jobs.filter((job) => job.status === "queued")
    .sort((left, right) => (left.queue_position ?? Infinity) - (right.queue_position ?? Infinity));
  return [...running, ...waiting];
}

export function visibleProjectHistory(projects: ProjectOverview[], jobs: GenerationJob[]): ProjectOverview[] {
  const activeIds = new Set(activeQueueJobs(jobs).map((job) => job.project_id));
  return projects.filter((project) => !activeIds.has(project.id));
}

/**
 * Recent Projects entries "Alle Projekte löschen" removes: every project and every
 * finished request that never created one (e.g. failed during research).
 * Active generations are never part of a bulk delete.
 */
export function deletableProjectCount(history: ProjectOverview[]): number {
  return history.filter((project) => !["queued", "running", "cancelling"].includes(project.status)).length;
}

/**
 * Video Queue rows: the active jobs, plus the ones the user cancelled from this
 * page, which stay visible as "Abgebrochen" (so the click visibly ends).
 */
export function queueDisplayJobs(jobs: GenerationJob[], cancelledHere: ReadonlySet<string>): GenerationJob[] {
  const active = activeQueueJobs(jobs);
  const stopped = jobs.filter((job) => job.status === "cancelled" && cancelledHere.has(job.id));
  return [...active.filter((job) => job.status !== "queued"), ...stopped, ...active.filter((job) => job.status === "queued")];
}

/** Recent Projects status line (the raw status, except the cancel states in German). */
export function historyStatusLabel(status: string): string {
  if (status === "cancelled") return "Abgebrochen";
  if (status === "cancelling") return "Wird abgebrochen…";
  return status.replaceAll("_", " ");
}
