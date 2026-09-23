import type { GenerationJob, ProjectOverview } from "./types";

export function activeQueueJobs(jobs: GenerationJob[]): GenerationJob[] {
  const running = jobs.filter((job) => job.status === "running");
  const waiting = jobs.filter((job) => job.status === "queued")
    .sort((left, right) => (left.queue_position ?? Infinity) - (right.queue_position ?? Infinity));
  return [...running, ...waiting];
}

export function visibleProjectHistory(projects: ProjectOverview[], jobs: GenerationJob[]): ProjectOverview[] {
  const activeIds = new Set(activeQueueJobs(jobs).map((job) => job.project_id));
  return projects.filter((project) => !activeIds.has(project.id));
}
