import type { GenerationJob } from "./types";

export const POLL_INTERVAL_MS = 2500;
export const POLL_TIMEOUT_MS = 12000;
/** Consecutive failed polls before the UI shows a (user-safe) problem. */
export const FAILURES_BEFORE_NOTICE = 2;
export const PROJECT_LOAD_ATTEMPTS = 3;

export type Timers = {
  setTimeout: (callback: () => void, ms: number) => unknown;
  clearTimeout: (handle: unknown) => void;
};

const browserTimers: Timers = {
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

export class PollTimeoutError extends Error {
  constructor() {
    super("The ClipForge API did not answer in time.");
    this.name = "PollTimeoutError";
  }
}

/** Run one request with an abort deadline; always settles (never hangs a poll loop). */
export function withTimeout<T>(run: (signal: AbortSignal) => Promise<T>, ms: number, timers: Timers = browserTimers): Promise<T> {
  const controller = new AbortController();
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const handle = timers.setTimeout(() => {
      if (settled) return;
      settled = true;
      controller.abort();
      reject(new PollTimeoutError());
    }, ms);
    let pending: Promise<T>;
    try {
      pending = Promise.resolve(run(controller.signal));
    } catch (reason) {
      pending = Promise.reject(reason);
    }
    pending.then(
      (value) => {
        if (settled) return;
        settled = true;
        timers.clearTimeout(handle);
        resolve(value);
      },
      (reason: unknown) => {
        if (settled) return;
        settled = true;
        timers.clearTimeout(handle);
        reject(reason);
      },
    );
  });
}

/** Percent shown for a job: 100 once completed, never moving backwards while running. */
export function jobProgressPercent(job: GenerationJob | null | undefined, previous = 0): number {
  if (!job) return previous;
  if (job.status === "completed") return 100;
  const value = Math.round(Math.max(0, Math.min(1, Number(job.progress) || 0)) * 100);
  return job.status === "running" ? Math.max(previous, Math.min(99, value)) : value;
}

/** A short, safe message for a failed poll (no stack traces or raw objects). */
export function pollErrorMessage(reason: unknown): string {
  if (reason instanceof PollTimeoutError || (reason instanceof Error && reason.name === "TimeoutError")) {
    return "Die Verbindung zur ClipForge-API ist langsam. Der Status wird weiter abgefragt …";
  }
  if (reason instanceof Error && reason.message && reason.name !== "TypeError") return reason.message;
  return "Der Erstellungsstatus konnte gerade nicht geladen werden. Es wird erneut versucht …";
}

export type GenerationWatcherOptions<P> = {
  /** Current job (``null`` when the backend has no job for it any more). */
  loadJob: (signal: AbortSignal) => Promise<GenerationJob | null>;
  /** The finished project (``null`` when it does not exist yet). */
  loadProject: (signal: AbortSignal) => Promise<P | null>;
  onJob?: (job: GenerationJob) => void;
  onCompleted: (project: P, job: GenerationJob | null) => void;
  onFailed?: (job: GenerationJob) => void;
  /** ``null`` clears a previously shown problem. */
  onError?: (message: string | null) => void;
  intervalMs?: number;
  timeoutMs?: number;
  timers?: Timers;
};

export type GenerationWatcher = {
  start: () => void;
  stop: () => void;
  readonly completed: boolean;
  readonly stopped: boolean;
};

/**
 * Poll one generation job until it is done, then load the project once.
 *
 * Polls are sequential (the next one is scheduled after the previous one
 * settled), bounded by a timeout and never reject: a slow or failing API can
 * delay the UI, but never freeze it on stale progress.  Completion is handled
 * exactly once, however many "completed" responses arrive.
 */
export function createGenerationWatcher<P>(options: GenerationWatcherOptions<P>): GenerationWatcher {
  const timers = options.timers ?? browserTimers;
  const interval = options.intervalMs ?? POLL_INTERVAL_MS;
  const timeout = options.timeoutMs ?? POLL_TIMEOUT_MS;
  let stopped = true;
  let running = false;
  let completed = false;
  let finishing = false;
  let failures = 0;
  let handle: unknown = null;

  function report(reason: unknown) {
    failures += 1;
    if (failures >= FAILURES_BEFORE_NOTICE) options.onError?.(pollErrorMessage(reason));
  }

  function schedule(ms: number) {
    if (stopped) return;
    handle = timers.setTimeout(() => {
      handle = null;
      void tick();
    }, ms);
  }

  async function finish(job: GenerationJob | null): Promise<boolean> {
    if (completed || finishing) return completed;
    finishing = true;
    try {
      for (let attempt = 0; attempt < PROJECT_LOAD_ATTEMPTS && !stopped; attempt += 1) {
        try {
          const project = await withTimeout(options.loadProject, timeout, timers);
          if (stopped) return false;
          if (project) {
            completed = true;
            stopped = true;
            failures = 0;
            options.onError?.(null);
            options.onCompleted(project, job);
            return true;
          }
        } catch (reason) {
          report(reason);
        }
      }
      return false;
    } finally {
      finishing = false;
    }
  }

  async function tick(): Promise<void> {
    if (stopped || running || completed) return;
    running = true;
    try {
      const job = await withTimeout(options.loadJob, timeout, timers);
      if (stopped) return;
      failures = 0;
      options.onError?.(null);
      if (job && (job.status === "failed" || job.status === "removed")) {
        stopped = true;
        options.onJob?.(job);
        options.onFailed?.(job);
        return;
      }
      if (job && job.status !== "completed") {
        options.onJob?.(job);
        return;
      }
      // Completed (or no job left): show 100 % at once, then open the project.
      if (job) options.onJob?.({ ...job, progress: 1 });
      await finish(job);
    } catch (reason) {
      report(reason);
    } finally {
      running = false;
      if (!stopped && !completed) schedule(interval);
    }
  }

  return {
    start() {
      if (completed || !stopped) return;
      stopped = false;
      void tick().catch(() => undefined);
    },
    stop() {
      stopped = true;
      if (handle !== null) timers.clearTimeout(handle);
      handle = null;
    },
    get completed() {
      return completed;
    },
    get stopped() {
      return stopped;
    },
  };
}
