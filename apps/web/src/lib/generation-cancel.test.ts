import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { applyJob, CANCEL_LABEL, CANCELLED_LABEL, CANCELLING_LABEL, cancelView, createCancelRequester, holdsWorker, markCancelling } from "./generation-cancel.ts";
import { createGenerationWatcher, jobProgressPercent, type Timers } from "./generation-poll.ts";
import { hasActiveJob } from "./home-poll.ts";
import { activeQueueJobs, deletableProjectCount, historyStatusLabel, queueDisplayJobs, visibleProjectHistory } from "./queue-overview.ts";
import type { GenerationJob, ProjectOverview } from "./types.ts";

const home = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
const projectPage = readFileSync(new URL("../components/project-page.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");

function job(id: string, status: GenerationJob["status"], overrides: Partial<GenerationJob> = {}): GenerationJob {
  return {
    id: `job-${id}`, project_id: id, prompt: `Frage ${id}?`, base_revision: null, status, current_stage: "rendering", stage_label: "Preparing scene video",
    progress: 0.42, completed_units: null, total_units: null, started_at: null, updated_at: "", completed_at: null, elapsed_seconds: 12,
    estimated_remaining_seconds: 30, failure_category: null, failure_message: null, queue_position: status === "queued" ? 1 : null, ...overrides,
  };
}

const flush = async () => {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
};

test("only the running job exposes Abbrechen; queued jobs keep Remove, finished ones show nothing", () => {
  assert.equal(cancelView(job("A", "running")), "cancel");
  assert.equal(cancelView(job("A", "cancelling")), "cancelling");
  assert.equal(cancelView(job("A", "cancelled")), "cancelled");
  for (const status of ["queued", "completed", "failed", "removed"] as const) assert.equal(cancelView(job("A", status)), null);
  assert.deepEqual([CANCEL_LABEL, CANCELLING_LABEL, CANCELLED_LABEL], ["Abbrechen", "Wird abgebrochen…", "Abgebrochen"]);
  // The queue renders Abbrechen for the active job and keeps Remove for waiting ones.
  assert.match(home, /view === "cancel" && <button[^>]*onClick=\{\(\) => onCancel\(item\.id\)\}[^>]*>\{CANCEL_LABEL\}/);
  assert.match(home, />Remove<\/button>/);
  assert.match(api, /`\/generation-jobs\/\$\{jobId\}\/cancel`, \{ method: "POST" \}/);
});

test("a click shows Wird abgebrochen… at once and sends exactly one request, however often clicked", async () => {
  const calls: string[] = [];
  const pendingStates: string[][] = [];
  const answered: GenerationJob[] = [];
  let resolve: (value: GenerationJob) => void = () => undefined;
  const requester = createCancelRequester({
    cancel: (jobId) => {
      calls.push(jobId);
      return new Promise((done) => { resolve = done; });
    },
    onPending: (pending) => pendingStates.push([...pending]),
    onJob: (next) => answered.push(next),
    onError: () => assert.fail("no error expected"),
  });
  const running = job("A", "running");
  assert.equal(requester.request(running.id), true);
  // Immediately: the pending id turns the running job's view into "cancelling" (and the optimistic list agrees).
  assert.equal(cancelView(running, requester.pending), "cancelling");
  assert.equal(markCancelling([running, job("B", "queued")], running.id)[0].status, "cancelling");
  assert.equal(markCancelling([job("B", "queued")], "job-B")[0].status, "queued");
  assert.equal(requester.request(running.id), false);
  assert.equal(requester.request(running.id), false);
  assert.deepEqual(calls, ["job-A"]);
  resolve(job("A", "cancelling"));
  await flush();
  assert.equal(answered[0].status, "cancelling");
  assert.deepEqual(pendingStates.at(-1), []);
  assert.equal(requester.pending.size, 0);
});

test("a failed cancel request is reported and can be retried", async () => {
  const errors: string[] = [];
  let attempts = 0;
  const requester = createCancelRequester({
    cancel: async () => {
      attempts += 1;
      throw new Error("API nicht erreichbar");
    },
    onPending: () => undefined,
    onJob: () => assert.fail("no job expected"),
    onError: (message) => errors.push(message),
  });
  requester.request("job-A");
  await flush();
  assert.deepEqual(errors, ["API nicht erreichbar"]);
  assert.equal(requester.request("job-A"), true);
  await flush();
  assert.equal(attempts, 2);
});

test("cancelling stays active until the worker stops; cancelled is terminal everywhere", () => {
  const cancelling = job("A", "cancelling");
  const cancelled = job("A", "cancelled");
  assert.equal(holdsWorker(cancelling), true);
  assert.equal(holdsWorker(cancelled), false);
  assert.equal(hasActiveJob([cancelling]), true); // polling continues while it stops
  assert.equal(hasActiveJob([cancelled]), false); // polling stops treating it as active
  assert.deepEqual(activeQueueJobs([job("B", "queued"), cancelling]).map((item) => item.id), ["job-A", "job-B"]);
  assert.deepEqual(activeQueueJobs([cancelled, job("B", "queued")]).map((item) => item.id), ["job-B"]);
  // The next queued job becomes the running one after the cancel.
  assert.deepEqual(activeQueueJobs([cancelled, job("B", "running")]).map((item) => item.status), ["running"]);
});

test("the Video Queue keeps a job cancelled from this page visible as Abgebrochen", () => {
  const jobs = [job("A", "cancelled"), job("C", "cancelled"), job("B", "running"), job("D", "queued")];
  const shown = queueDisplayJobs(jobs, new Set(["job-A"]));
  assert.deepEqual(shown.map((item) => [item.id, item.status]), [["job-B", "running"], ["job-A", "cancelled"], ["job-D", "queued"]]);
  assert.match(home, /stopped \? CANCELLED_LABEL/);
  assert.match(home, /Das Projekt bleibt erhalten\./);
});

test("Recent Projects label the cancel states and never bulk-count a stopping job", () => {
  assert.equal(historyStatusLabel("cancelled"), "Abgebrochen");
  assert.equal(historyStatusLabel("cancelling"), "Wird abgebrochen…");
  assert.equal(historyStatusLabel("needs_fix"), "needs fix");
  const history: ProjectOverview[] = [
    { id: "A", title: "A", status: "cancelled", current_revision: 1, created_at: "", updated_at: "" },
    { id: "B", title: "B", status: "cancelling", current_revision: null, created_at: "", updated_at: "" },
  ];
  assert.equal(deletableProjectCount(history), 1);
  assert.deepEqual(visibleProjectHistory(history, [job("B", "cancelling")]).map((item) => item.id), ["A"]);
  assert.match(home, /historyStatusLabel\(project\.status\)/);
});

test("the project page watcher treats cancelled as terminal and keeps polling while cancelling", async () => {
  // Only the poll interval (1000 ms) is fired; request timeouts (99 s) never are.
  const scheduled: Array<{ callback: () => void; ms: number }> = [];
  const timers: Timers = { setTimeout: (callback, ms) => { scheduled.push({ callback, ms }); return scheduled.length; }, clearTimeout: () => undefined };
  const pending = {
    shift: () => { const index = scheduled.findIndex((entry) => entry.ms === 1000); return index < 0 ? undefined : scheduled.splice(index, 1)[0].callback; },
    get length() { return scheduled.filter((entry) => entry.ms === 1000).length; },
  };
  const responses = [job("A", "running"), job("A", "cancelling"), job("A", "cancelled")];
  let calls = 0;
  const failed: GenerationJob[] = [];
  const seen: string[] = [];
  const watcher = createGenerationWatcher<{ id: string }>({
    loadJob: async () => responses[Math.min(calls++, responses.length - 1)],
    loadProject: async () => assert.fail("a cancelled job does not complete"),
    onJob: (next) => seen.push(next.status),
    onFailed: (stopped) => failed.push(stopped),
    onCompleted: () => assert.fail("never completed"),
    timers,
    intervalMs: 1000,
    timeoutMs: 99_000,
  });
  watcher.start();
  await flush();
  pending.shift()?.();
  await flush();
  pending.shift()?.();
  await flush();
  assert.deepEqual(seen, ["running", "cancelling", "cancelled"]);
  assert.equal(failed[0].status, "cancelled");
  assert.equal(watcher.stopped, true);
  assert.equal(pending.length, 0); // no further polling
  assert.equal(jobProgressPercent(job("A", "cancelling", { progress: 0.1 }), 42), 42); // never jumps back
  assert.match(projectPage, /failed\.status !== "cancelled"/); // a kept project opens after a cancel
  assert.match(projectPage, /CANCELLING_LABEL/);
});

test("the server's answer replaces the optimistic job in place", () => {
  const list = [job("A", "cancelling"), job("B", "queued")];
  const next = applyJob(list, job("A", "cancelled"));
  assert.deepEqual(next.map((item) => item.status), ["cancelled", "queued"]);
  assert.equal(applyJob(list, job("Z", "cancelled")), list);
});
