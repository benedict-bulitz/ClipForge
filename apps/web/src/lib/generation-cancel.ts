/**
 * Cancelling the RUNNING generation from the UI.
 *
 * The backend status is the truth (running -> cancelling -> cancelled); the UI
 * shows "Wird abgebrochen…" the moment the user clicks, sends one request per
 * job however often the button is pressed, and ends at "Abgebrochen" once the
 * worker stopped.  Queued jobs keep their own action (remove from the queue).
 */
import type { GenerationJob } from "./types";

export const CANCEL_LABEL = "Abbrechen";
export const CANCELLING_LABEL = "Wird abgebrochen…";
export const CANCELLED_LABEL = "Abgebrochen";

export type CancelView = "cancel" | "cancelling" | "cancelled" | null;

/** Which cancel affordance a job shows (null: none - queued jobs are removed, finished ones are done). */
export function cancelView(job: GenerationJob, pending: ReadonlySet<string> = new Set()): CancelView {
  if (job.status === "cancelled") return "cancelled";
  if (job.status === "cancelling") return "cancelling";
  if (job.status === "running") return pending.has(job.id) ? "cancelling" : "cancel";
  return null;
}

/** The job holds the one worker: running, or still stopping after a cancel. */
export function holdsWorker(job: GenerationJob): boolean {
  return job.status === "running" || job.status === "cancelling";
}

/** Optimistic UI: the clicked running job shows "cancelling" immediately. */
export function markCancelling(jobs: GenerationJob[], jobId: string): GenerationJob[] {
  return jobs.map((job) => (job.id === jobId && job.status === "running" ? { ...job, status: "cancelling" } : job));
}

/** Replace one job with the server's answer (keeps the list order). */
export function applyJob(jobs: GenerationJob[], next: GenerationJob): GenerationJob[] {
  return jobs.some((job) => job.id === next.id) ? jobs.map((job) => (job.id === next.id ? next : job)) : jobs;
}

export type CancelRequesterOptions = {
  cancel: (jobId: string) => Promise<GenerationJob>;
  onPending: (pending: ReadonlySet<string>) => void;
  onJob: (job: GenerationJob) => void;
  onError: (message: string) => void;
};

export type CancelRequester = {
  /** false when a request for this job is already on its way (duplicate click). */
  request: (jobId: string) => boolean;
  readonly pending: ReadonlySet<string>;
};

export function createCancelRequester(options: CancelRequesterOptions): CancelRequester {
  const pending = new Set<string>();
  function publish() {
    options.onPending(new Set(pending));
  }
  return {
    request(jobId) {
      if (pending.has(jobId)) return false;
      pending.add(jobId);
      publish();
      void options
        .cancel(jobId)
        .then((job) => options.onJob(job))
        .catch((reason: unknown) => {
          options.onError(reason instanceof Error && reason.message ? reason.message : "Die Erstellung konnte nicht abgebrochen werden.");
        })
        .finally(() => {
          pending.delete(jobId);
          publish();
        });
      return true;
    },
    get pending() {
      return pending;
    },
  };
}
