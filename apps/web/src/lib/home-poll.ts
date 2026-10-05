/**
 * The home page's one polling authority for the generation queue and the
 * project history.
 *
 * - Jobs: every ACTIVE_POLL_MS while a job is queued or running (progress UI);
 *   otherwise a low-frequency IDLE_POLL_MS heartbeat, so a job started in
 *   another tab is still noticed eventually.
 * - Projects: event-driven only - on start, when the active queue drains (a
 *   generation completed or failed), after explicit mutations
 *   (``refreshAll``/``refreshProjects``) and when the page becomes visible
 *   or focused again. Never on a timer.
 * - Hidden tab: no timer at all; becoming visible (or focused) refreshes once
 *   immediately and resumes the active/idle cadence.
 *
 * Exactly one timer exists at a time, requests never overlap (a request made
 * while one is in flight joins it), and ``stop`` cancels the timer and the
 * visibility listeners, so a React Strict Mode remount leaves one poller.
 */
import type { GenerationJob, ProjectOverview } from "./types";
import type { Timers } from "./generation-poll";

export const ACTIVE_POLL_MS = 2500;
export const IDLE_POLL_MS = 60_000;
/** A focus event right after a visibility change (tab switch) must not refresh twice. */
export const FOCUS_REFRESH_GAP_MS = 1500;

export type PageVisibility = {
  hidden: () => boolean;
  /** Calls ``onVisible`` when the page becomes visible or regains focus; returns an unsubscribe. */
  subscribe: (onVisible: () => void, onHidden: () => void) => () => void;
};

/**
 * ``P`` is the event-driven payload: the project history on the home page, the
 * Queue Overview's project summaries on ``/queue`` (the same authority, one
 * instance per page).
 */
export type HomePollerOptions<P = ProjectOverview[]> = {
  loadJobs: () => Promise<GenerationJob[]>;
  loadProjects: () => Promise<P>;
  onJobs: (jobs: GenerationJob[]) => void;
  onProjects: (projects: P) => void;
  timers?: Timers;
  visibility?: PageVisibility;
  now?: () => number;
  activeMs?: number;
  idleMs?: number;
};

export type HomePoller = {
  start: () => void;
  stop: () => void;
  /** Jobs + projects now (after creating, deleting or queue changes). */
  refreshAll: () => Promise<void>;
  refreshProjects: () => Promise<void>;
  readonly mode: "active" | "idle" | "hidden" | "stopped";
};

export function hasActiveJob(jobs: GenerationJob[]): boolean {
  // "cancelling" is still active (the worker is stopping); "cancelled" is terminal.
  return jobs.some((job) => job.status === "queued" || job.status === "running" || job.status === "cancelling");
}

const browserTimers: Timers = {
  setTimeout: (callback, ms) => globalThis.setTimeout(callback, ms),
  clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** document.visibilityState + window focus, for the browser. */
export const browserVisibility: PageVisibility = {
  hidden: () => typeof document !== "undefined" && document.visibilityState === "hidden",
  subscribe: (onVisible, onHidden) => {
    if (typeof document === "undefined" || typeof window === "undefined") return () => {};
    const change = () => (document.visibilityState === "hidden" ? onHidden() : onVisible());
    document.addEventListener("visibilitychange", change);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", change);
      window.removeEventListener("focus", onVisible);
    };
  },
};

export function createHomePoller<P = ProjectOverview[]>(options: HomePollerOptions<P>): HomePoller {
  const timers = options.timers ?? browserTimers;
  const visibility = options.visibility ?? browserVisibility;
  const now = options.now ?? (() => Date.now());
  const activeMs = options.activeMs ?? ACTIVE_POLL_MS;
  const idleMs = options.idleMs ?? IDLE_POLL_MS;
  let timer: unknown = null;
  let started = false;
  let stopped = false;
  let active = false;
  let lastRefresh = -Infinity;
  let jobsInFlight: Promise<void> | null = null;
  let projectsInFlight: Promise<void> | null = null;
  let unsubscribe: (() => void) | null = null;

  function clear() {
    if (timer !== null) timers.clearTimeout(timer);
    timer = null;
  }

  function schedule() {
    clear();
    if (stopped || visibility.hidden()) return;
    timer = timers.setTimeout(() => {
      timer = null;
      void refreshJobs();
    }, active ? activeMs : idleMs);
  }

  function refreshProjects(): Promise<void> {
    if (stopped) return Promise.resolve();
    if (projectsInFlight) return projectsInFlight;
    projectsInFlight = options.loadProjects()
      .then((projects) => { if (!stopped) options.onProjects(projects); })
      .catch(() => { /* keep the last known history; the next event retries */ })
      .finally(() => { projectsInFlight = null; });
    return projectsInFlight;
  }

  function refreshJobs(): Promise<void> {
    if (stopped) return Promise.resolve();
    if (jobsInFlight) return jobsInFlight;
    clear();
    jobsInFlight = options.loadJobs()
      .then((jobs) => {
        if (stopped) return;
        const wasActive = active;
        active = hasActiveJob(jobs);
        options.onJobs(jobs);
        // The queue drained: a generation completed or failed, so the history changed.
        if (wasActive && !active) void refreshProjects();
      })
      .catch(() => { /* keep the last known queue; the cadence below retries */ })
      .finally(() => {
        jobsInFlight = null;
        schedule();
      });
    return jobsInFlight;
  }

  function refreshAll(): Promise<void> {
    lastRefresh = now();
    return Promise.all([refreshJobs(), refreshProjects()]).then(() => undefined);
  }

  function onVisible() {
    if (stopped || visibility.hidden()) return;
    if (now() - lastRefresh < FOCUS_REFRESH_GAP_MS) {
      if (timer === null && !jobsInFlight) schedule();
      return;
    }
    void refreshAll();
  }

  return {
    start() {
      if (started || stopped) return;
      started = true;
      unsubscribe = visibility.subscribe(onVisible, clear);
      if (visibility.hidden()) {
        void refreshProjects(); // render the history once; jobs resume when visible
        return;
      }
      void refreshAll();
    },
    stop() {
      stopped = true;
      clear();
      unsubscribe?.();
      unsubscribe = null;
    },
    refreshAll,
    refreshProjects,
    get mode() {
      if (stopped) return "stopped";
      if (visibility.hidden()) return "hidden";
      return active ? "active" : "idle";
    },
  };
}
