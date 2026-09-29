import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { ACTIVE_POLL_MS, FOCUS_REFRESH_GAP_MS, IDLE_POLL_MS, createHomePoller, hasActiveJob, type PageVisibility } from "./home-poll.ts";
import type { Timers } from "./generation-poll.ts";
import type { GenerationJob, ProjectOverview } from "./types.ts";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");

/** A virtual clock: timers only fire when the test advances time. */
function fakeClock() {
  let now = 0;
  let nextId = 1;
  const pending = new Map<number, { at: number; callback: () => void }>();
  const timers: Timers = {
    setTimeout: (callback, ms) => { const id = nextId++; pending.set(id, { at: now + ms, callback }); return id; },
    clearTimeout: (handle) => { pending.delete(handle as number); },
  };
  const flush = () => new Promise<void>((resolve) => setImmediate(resolve));
  return {
    timers,
    now: () => now,
    pendingCount: () => pending.size,
    async advance(ms: number) {
      const target = now + ms;
      for (;;) {
        await flush();
        const due = [...pending.entries()].filter(([, item]) => item.at <= target).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        pending.delete(due[0]);
        now = due[1].at;
        due[1].callback();
      }
      now = target;
      await flush();
    },
    flush,
  };
}

function fakeVisibility() {
  let hidden = false;
  const listeners = new Set<{ visible: () => void; hidden: () => void }>();
  const visibility: PageVisibility = {
    hidden: () => hidden,
    subscribe: (onVisible, onHidden) => {
      const entry = { visible: onVisible, hidden: onHidden };
      listeners.add(entry);
      return () => listeners.delete(entry);
    },
  };
  return {
    visibility,
    listenerCount: () => listeners.size,
    hide() { hidden = true; listeners.forEach((item) => item.hidden()); },
    show() { hidden = false; listeners.forEach((item) => item.visible()); },
    focus() { listeners.forEach((item) => item.visible()); },
  };
}

const job = (status: GenerationJob["status"]): GenerationJob => ({ id: `j-${status}`, project_id: "p1", prompt: "Why?", status, progress: 0.3 } as unknown as GenerationJob);
const project: ProjectOverview = { id: "p0", title: "Why cats purr", status: "rendered", current_revision: 1, created_at: "", updated_at: "" };

function setup(initialJobs: GenerationJob[] = []) {
  const clock = fakeClock();
  const page = fakeVisibility();
  const state = { jobs: initialJobs, jobCalls: 0, projectCalls: 0, received: [] as GenerationJob[][] };
  const poller = createHomePoller({
    loadJobs: async () => { state.jobCalls += 1; return state.jobs; },
    loadProjects: async () => { state.projectCalls += 1; return [project]; },
    onJobs: (jobs) => state.received.push(jobs),
    onProjects: () => {},
    timers: clock.timers,
    visibility: page.visibility,
    now: clock.now,
  });
  return { clock, page, state, poller };
}

test("hasActiveJob: queued and running are active; finished jobs are not", () => {
  assert.equal(hasActiveJob([job("queued")]), true);
  assert.equal(hasActiveJob([job("running")]), true);
  assert.equal(hasActiveJob([job("completed"), job("failed")]), false);
  assert.equal(hasActiveJob([]), false);
});

test("ACTIVE: a queued/running job keeps responsive job polling", async () => {
  const { clock, state, poller } = setup([job("running")]);
  poller.start();
  await clock.flush();
  assert.equal(state.jobCalls, 1);
  await clock.advance(ACTIVE_POLL_MS * 4);
  assert.equal(state.jobCalls, 5);
  assert.equal(poller.mode, "active");
  assert.equal(state.projectCalls, 1, "the history is not re-fetched on every job poll");
  poller.stop();
});

test("IDLE: no job means no aggressive polling, only a slow heartbeat for other tabs", async () => {
  const { clock, state, poller } = setup([]);
  poller.start();
  await clock.advance(IDLE_POLL_MS - 1);
  assert.equal(state.jobCalls, 1, "nothing between the first load and the heartbeat");
  assert.equal(state.projectCalls, 1);
  await clock.advance(1);
  assert.equal(state.jobCalls, 2);
  await clock.advance(IDLE_POLL_MS * 4);
  assert.equal(state.jobCalls, 6, "at most one request per idle interval");
  assert.equal(state.projectCalls, 1, "the project overview is never polled on a timer");
  assert.equal(poller.mode, "idle");
  poller.stop();
});

test("a job started elsewhere is detected by the heartbeat and switches to active polling", async () => {
  const { clock, state, poller } = setup([]);
  poller.start();
  await clock.flush();
  state.jobs = [job("queued")];
  await clock.advance(IDLE_POLL_MS);
  assert.equal(poller.mode, "active");
  const before = state.jobCalls;
  await clock.advance(ACTIVE_POLL_MS);
  assert.equal(state.jobCalls, before + 1);
  poller.stop();
});

test("completion: when the queue drains, the history refreshes once and polling calms down", async () => {
  const { clock, state, poller } = setup([job("running")]);
  poller.start();
  await clock.advance(ACTIVE_POLL_MS);
  assert.equal(state.projectCalls, 1);
  state.jobs = [];
  await clock.advance(ACTIVE_POLL_MS);
  assert.equal(state.projectCalls, 2, "the finished project appears in Recent Projects");
  assert.equal(poller.mode, "idle");
  const calls = state.jobCalls;
  await clock.advance(ACTIVE_POLL_MS * 10);
  assert.equal(state.jobCalls, calls, "no more fast polling after completion");
  poller.stop();
});

test("mutations (create, delete, bulk delete) refresh jobs and history immediately", async () => {
  const { clock, state, poller } = setup([]);
  poller.start();
  await clock.flush();
  state.jobs = [job("queued")];
  await poller.refreshAll();
  assert.equal(state.projectCalls, 2);
  assert.equal(poller.mode, "active", "a new generation resumes responsive polling at once");
  await poller.refreshProjects();
  assert.equal(state.projectCalls, 3);
  poller.stop();
});

test("HIDDEN: polling pauses; VISIBLE: exactly one immediate refresh, then the right cadence", async () => {
  const { clock, page, state, poller } = setup([job("running")]);
  poller.start();
  await clock.flush();
  page.hide();
  assert.equal(poller.mode, "hidden");
  assert.equal(clock.pendingCount(), 0, "no timer while hidden");
  await clock.advance(ACTIVE_POLL_MS * 20);
  assert.equal(state.jobCalls, 1);
  assert.equal(state.projectCalls, 1);
  page.show();
  await clock.flush();
  assert.equal(state.jobCalls, 2, "one immediate job refresh");
  assert.equal(state.projectCalls, 2, "one immediate history refresh");
  page.focus(); // the focus event that follows a tab switch
  await clock.flush();
  assert.equal(state.jobCalls, 2, "focus right after becoming visible does not refresh twice");
  await clock.advance(ACTIVE_POLL_MS);
  assert.equal(state.jobCalls, 3, "active cadence resumes");
  poller.stop();
});

test("FOCUS: regaining focus later refreshes once", async () => {
  const { clock, page, state, poller } = setup([]);
  poller.start();
  await clock.advance(FOCUS_REFRESH_GAP_MS + 10);
  page.focus();
  await clock.flush();
  assert.equal(state.jobCalls, 2);
  assert.equal(state.projectCalls, 2);
  assert.equal(clock.pendingCount(), 1, "still one timer");
  poller.stop();
});

test("a request never overlaps itself: refreshes during a request join it", async () => {
  const clock = fakeClock();
  let release!: (jobs: GenerationJob[]) => void;
  let calls = 0;
  const poller = createHomePoller({
    loadJobs: () => { calls += 1; return new Promise<GenerationJob[]>((resolve) => { release = resolve; }); },
    loadProjects: async () => [],
    onJobs: () => {},
    onProjects: () => {},
    timers: clock.timers,
    visibility: fakeVisibility().visibility,
    now: clock.now,
  });
  poller.start();
  void poller.refreshAll();
  void poller.refreshAll();
  await clock.flush();
  assert.equal(calls, 1);
  release([]);
  await clock.flush();
  assert.equal(clock.pendingCount(), 1);
  poller.stop();
});

test("UNMOUNT: stop clears the timer and the listeners; late answers are ignored", async () => {
  const { clock, page, state, poller } = setup([job("running")]);
  poller.start();
  await clock.flush();
  assert.equal(page.listenerCount(), 1);
  poller.stop();
  assert.equal(clock.pendingCount(), 0);
  assert.equal(page.listenerCount(), 0);
  await clock.advance(ACTIVE_POLL_MS * 10);
  page.show();
  await clock.flush();
  assert.equal(state.jobCalls, 1);
  assert.equal(poller.mode, "stopped");
});

test("STRICT MODE: mount, unmount, mount leaves exactly one polling chain", async () => {
  const clock = fakeClock();
  const page = fakeVisibility();
  let jobCalls = 0;
  const make = () => createHomePoller({
    loadJobs: async () => { jobCalls += 1; return [job("running")]; },
    loadProjects: async () => [],
    onJobs: () => {},
    onProjects: () => {},
    timers: clock.timers,
    visibility: page.visibility,
    now: clock.now,
  });
  const first = make();
  first.start();
  first.stop(); // React's simulated unmount
  const second = make();
  second.start();
  await clock.flush();
  const initial = jobCalls; // at most one initial request per mount
  await clock.advance(ACTIVE_POLL_MS * 4);
  assert.equal(jobCalls - initial, 4, "one chain: one request per interval");
  assert.equal(clock.pendingCount(), 1);
  assert.equal(page.listenerCount(), 1);
  second.stop();
});

test("the home page uses the poller as its only polling authority", () => {
  assert.match(home, /createHomePoller\(\{/);
  assert.match(home, /instance\.start\(\);/);
  assert.match(home, /return \(\) => \{\n\s+instance\.stop\(\);/);
  // the old fixed 2.5 s loop of both endpoints is gone
  assert.doesNotMatch(home, /setTimeout\(\(\) => void poll\(\), 2500\)/);
  assert.doesNotMatch(home, /async function poll\(\)/);
  assert.equal((home.match(/listProjectOverview\(/g) ?? []).length, 1);
  assert.equal((home.match(/listGenerationJobs\(/g) ?? []).length, 1);
  // mutations refresh through the same authority
  assert.match(home, /async function refresh\(\) \{\n\s+await poller\.current\?\.refreshAll\(\);/);
});
