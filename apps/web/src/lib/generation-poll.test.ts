import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createGenerationWatcher, jobProgressPercent, PollTimeoutError, withTimeout, type Timers } from "./generation-poll.ts";
import type { GenerationJob } from "./types.ts";

const projectPage = readFileSync(new URL("../components/project-page.tsx", import.meta.url), "utf8");
const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");

/** Manual clock: timers fire only when the test says so. */
function fakeTimers() {
  let next = 1;
  const pending = new Map<number, { callback: () => void; ms: number }>();
  const timers: Timers = {
    setTimeout: (callback, ms) => {
      const id = next++;
      pending.set(id, { callback, ms });
      return id;
    },
    clearTimeout: (handle) => {
      pending.delete(handle as number);
    },
  };
  return {
    timers,
    pending,
    /** Fire every timer with at most ``ms`` delay (poll interval or timeout). */
    fire(ms?: number) {
      for (const [id, entry] of [...pending]) {
        if (ms === undefined || entry.ms === ms) {
          pending.delete(id);
          entry.callback();
        }
      }
    },
  };
}

const flush = async () => {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
};

function job(status: GenerationJob["status"], progress: number, overrides: Partial<GenerationJob> = {}): GenerationJob {
  return {
    id: "job-1", project_id: "p-1", prompt: "Warum?", base_revision: null, status, current_stage: status === "completed" ? "complete" : "rendering",
    stage_label: "", progress, completed_units: null, total_units: null, started_at: null, updated_at: "", completed_at: null,
    elapsed_seconds: 0, estimated_remaining_seconds: null, failure_category: null, failure_message: null, queue_position: null, ...overrides,
  };
}

function watch(responses: Array<() => Promise<GenerationJob | null>>, projectResponses: Array<() => Promise<{ id: string } | null>> = [async () => ({ id: "p-1" })]) {
  const clock = fakeTimers();
  const events = { jobs: [] as GenerationJob[], progress: [] as number[], completed: [] as string[], errors: [] as Array<string | null>, jobCalls: 0, projectCalls: 0 };
  let shown = 0;
  const watcher = createGenerationWatcher<{ id: string }>({
    loadJob: () => {
      const response = responses[Math.min(events.jobCalls, responses.length - 1)];
      events.jobCalls += 1;
      return response();
    },
    loadProject: () => {
      const response = projectResponses[Math.min(events.projectCalls, projectResponses.length - 1)];
      events.projectCalls += 1;
      return response();
    },
    onJob: (next) => {
      events.jobs.push(next);
      shown = jobProgressPercent(next, shown);
      events.progress.push(shown);
    },
    onCompleted: (project) => events.completed.push(project.id),
    onError: (message) => events.errors.push(message),
    timers: clock.timers,
    intervalMs: 2500,
    timeoutMs: 12000,
  });
  return { clock, events, watcher };
}

test("running -> completed: progress jumps to 100, polling stops, the project opens once", async () => {
  const { clock, events, watcher } = watch([async () => job("running", 0.41), async () => job("running", 0.67), async () => job("completed", 1)]);
  watcher.start();
  await flush();
  clock.fire(2500);
  await flush();
  clock.fire(2500);
  await flush();
  assert.deepEqual(events.progress, [41, 67, 100]);
  assert.deepEqual(events.completed, ["p-1"]);
  assert.equal(watcher.completed, true);
  assert.equal(clock.pending.size, 0); // no further poll scheduled
  clock.fire();
  await flush();
  assert.equal(events.jobCalls, 3);
});

test("completed while the UI still shows stale progress: shown as 100 immediately", async () => {
  // The API can report completion with a stale progress value.
  assert.equal(jobProgressPercent(job("completed", 0.67), 67), 100);
  const { clock, events, watcher } = watch([async () => job("running", 0.67), async () => job("completed", 0.67, { current_stage: "complete" })]);
  watcher.start();
  await flush();
  clock.fire(2500);
  await flush();
  assert.equal(events.jobs.at(-1)?.progress, 1);
  assert.deepEqual(events.progress, [67, 100]);
  assert.deepEqual(events.completed, ["p-1"]);
});

test("progress never moves backwards while running", () => {
  assert.equal(jobProgressPercent(job("running", 0.5), 67), 67);
  assert.equal(jobProgressPercent(job("running", 1), 0), 99); // 100 only when completed
});

test("a polling timeout is handled, surfaced once it repeats, and polling recovers", async () => {
  let hung = true;
  const { clock, events, watcher } = watch([
    () => new Promise(() => undefined), // never answers
    () => new Promise(() => undefined),
    async () => { hung = false; return job("completed", 1); },
  ]);
  watcher.start();
  await flush();
  clock.fire(12000); // first timeout
  await flush();
  assert.deepEqual(events.errors, []); // a single slow poll is not an error yet
  clock.fire(2500);
  await flush();
  clock.fire(12000); // second consecutive timeout
  await flush();
  assert.match(String(events.errors.at(-1)), /langsam/);
  clock.fire(2500);
  await flush();
  assert.equal(hung, false);
  assert.deepEqual(events.completed, ["p-1"]);
  assert.equal(events.errors.at(-1), null); // the notice clears on success
});

test("withTimeout aborts the request and rejects with a PollTimeoutError", async () => {
  const clock = fakeTimers();
  let aborted = false;
  const pending = withTimeout((signal) => {
    signal.addEventListener("abort", () => { aborted = true; });
    return new Promise(() => undefined);
  }, 5000, clock.timers);
  clock.fire(5000);
  await assert.rejects(pending, PollTimeoutError);
  assert.equal(aborted, true);
});

test("rejected polling promises never become unhandled rejections", async () => {
  const unhandled: unknown[] = [];
  const listener = (reason: unknown) => unhandled.push(reason);
  process.on("unhandledRejection", listener);
  try {
    const { clock, events, watcher } = watch([
      async () => { throw new TypeError("Cannot call a class as a function"); },
      async () => { throw undefined; },
      async () => job("running", 0.8),
      async () => job("completed", 1),
    ]);
    watcher.start();
    for (let round = 0; round < 3; round += 1) {
      await flush();
      clock.fire(2500);
    }
    await flush();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(unhandled, []);
    // Surfaced in user-safe words (no raw TypeError text), then cleared.
    assert.ok(events.errors.some((message) => message && !/class/.test(message)));
    assert.deepEqual(events.completed, ["p-1"]);
  } finally {
    process.off("unhandledRejection", listener);
  }
});

test("duplicate completion events open the project exactly once", async () => {
  const { clock, events, watcher } = watch(
    [async () => job("completed", 1), async () => job("completed", 1)],
    [async () => { throw new Error("busy"); }, async () => ({ id: "p-1" })],
  );
  watcher.start();
  watcher.start(); // a second start (e.g. re-render) is ignored
  await flush();
  clock.fire(2500);
  await flush();
  clock.fire();
  await flush();
  assert.deepEqual(events.completed, ["p-1"]);
  assert.equal(events.projectCalls, 2); // one retry after the failed project load
  assert.equal(clock.pending.size, 0);
});

test("a failed job stops polling and is reported", async () => {
  const failed: string[] = [];
  const clock = fakeTimers();
  const watcher = createGenerationWatcher({
    loadJob: async () => job("failed", 0.3, { failure_message: "Voice failed" }),
    loadProject: async () => null,
    onCompleted: () => assert.fail("must not open"),
    onFailed: (next) => failed.push(String(next.failure_message)),
    timers: clock.timers,
  });
  watcher.start();
  await flush();
  assert.deepEqual(failed, ["Voice failed"]);
  assert.equal(clock.pending.size, 0);
});

test("pages use the watcher and open the finished project without a reload", () => {
  assert.match(projectPage, /createGenerationWatcher/);
  assert.doesNotMatch(projectPage, /setInterval/); // no overlapping polls
  assert.match(projectPage, /watcher\.stop\(\)/);
  assert.match(projectPage, /jobProgressPercent/);
  assert.match(home, /openWhenComplete\(started\)/);
  assert.match(home, /router\.push\(`\/projects\/\$\{project\.id\}`\)/);
  assert.match(home, /withTimeout\(\(signal\) => listGenerationJobs\(signal\)/);
});
